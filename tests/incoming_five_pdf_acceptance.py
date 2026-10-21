"""Explicit network-blocked integration acceptance with five retained PDFs."""
import argparse
import json
import socket
import sqlite3
from pathlib import Path
from unittest.mock import patch

from incoming_documents import IncomingStore
from offline_review import ReviewError, digest
from test_incoming_integration import IncomingIntegrationTests


def hashes(root):
    return {str(path.relative_to(root)): digest(path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def main(source, output):
    output.mkdir(parents=False)
    original_hashes = hashes(source)
    case = IncomingIntegrationTests()
    IncomingIntegrationTests.setUpClass()
    outcomes = []
    try:
        case.setUp()
        case.host._incoming_costs_supported = True
        case.store.close()
        case.store = IncomingStore(output / "incoming-profile", case.host)
        case.addCleanup(case.store.close)
        case.store.import_snapshot(source)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("External network prohibited")):
            for doc in case.store.documents():
                document_id = doc["id"]
                revision, payload = case.store.latest(document_id)
                assert payload["object_id"] is None
                kind = case.store.base(document_id)["document_type"]
                payload.update(confirmed_type=kind, object_id=case.object_id, original_checked=True)
                revision = case.store.save(document_id, payload, revision)
                if document_id == 4:
                    for _ in range(2):
                        try:
                            case.store.transfer(document_id, revision)
                        except ReviewError as error:
                            reason = str(error)
                        else:
                            raise AssertionError("Unsupported document transferred")
                    assert case.store.card(document_id) is None
                    outcomes.append({"id": document_id, "state": "blocked", "reason": reason})
                    continue
                card = case.store.transfer(document_id, revision)
                assert case.store.transfer(document_id, revision) == card
                case.mode = "lost_ack"
                assert case.sync()["synced"] == 0, case.last_sync
                assert case.sync()["synced"] == 1, case.last_sync
                outcomes.append({"id": document_id, "state": "synced", "client_id": card["client_id"],
                                 "sha256": card["sha256"]})
            assert case.sync()["synced"] == 0
            response = case.server_client.get("/api/expense-documents",
                                             headers={"X-Sync-Key": "isolated-sync-test-key"})
            documents = response.get_json()["documents"]
            assert len(documents) == 4
            receipt_card = case.store.card(2)
            receipt = next(doc for doc in documents if doc["client_id"] == receipt_card["client_id"])
            assert receipt["invoice_number"] == "" and receipt["gross_amount"] == "7506.00"
            assert receipt["amount_due"] is None
            for result in outcomes:
                if result["state"] == "synced":
                    document = next(doc for doc in documents if doc["client_id"] == result["client_id"])
                    assert document["has_original"]
                    assert result["sha256"] in {item["sha256"] for item in document["attachments"]}
                    original = case.server_client.get(
                        f"/api/expense-documents/{document['id']}/original",
                        headers={"X-Sync-Key": "isolated-sync-test-key"})
                    assert original.status_code == 200
                    assert digest(original.data) == result["sha256"]
                    original.close()
            receipt = case.store.latest(2)[1]
            assert receipt["invoice_number"] is None and receipt["amount_due"] is None
            assert hashes(source) == original_hashes
            report = {"network": "blocked", "source_unchanged": True, "source_hashes": original_hashes,
                      "outcomes": outcomes, "normal_register_cards": 4, "receipt_debt_created": False,
                      "server_documents": documents}
            (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            for name, connect in (("desktop", case.desktop.get_db_connection),
                                  ("server", case.server.get_db_connection)):
                source_db = connect()
                target_db = sqlite3.connect(output / (name + ".db"))
                source_db.backup(target_db)
                target_db.close()
                source_db.close()
            print("PASS: 5 original PDFs; 3 invoices + 1 receipt -> normal outbox -> isolated ordinary server registry.")
            print("PASS: each lost ACK retried -> still 4 cards; all original download hashes match.")
            print("PASS: receipt without invoice number/debt; old Nordea invoice label blocked.")
            print("PASS: socket connections blocked; all source files unchanged.")
    finally:
        case.doCleanups()
        IncomingIntegrationTests.tearDownClass()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.snapshot.resolve(), args.output.resolve())
