import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from desktop_mail import DesktopMailStore
from mail_import import MailImportError


class DesktopMailStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = [{"id": 102, "name": "Isolated object", "markning": "TEST"}]
        self.store = DesktopMailStore(self.root, lambda: self.catalog)
        self.addCleanup(lambda: self.store.close())
        self.payload = {
            "document_type": "invoice", "supplier_name": "Test Supplier AB", "invoice_number": "123",
            "document_date": "2026-10-21", "currency_code": "SEK", "net_amount": "100.00",
            "vat_amount": "25.00", "gross_amount": "125.00", "rounding_amount": "",
            "amount_due": "125.00", "payment_reference": "00123", "allocation_basis": "net",
            "verification_status": "confirmed",
            "allocations": [{"destination": "object", "object_id": 102, "net_amount": "100.00"}],
        }
        self.accept(b"isolated file", 1)

    def accept(self, content, uid):
        return self.store.queue.accept(content, ".pdf", {
            "uidvalidity": "test", "uid": str(uid), "part": 1, "message_id": "",
            "sender": "", "subject": "", "received_at": "2026-10-21T00:00:00+00:00",
            "filename": "test.pdf", "content_type": "application/pdf",
        }, recognizer=lambda paths: {
            "fields": {**self.payload, "raw_text": "Invoice 123"},
            "state": "requires_review", "errors": [],
        })

    def test_draft_has_no_handoff_and_uses_real_catalog_only(self):
        self.assertEqual(self.store.objects(), self.catalog)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM trial_objects").fetchone()[0], 0)
        draft = {**self.payload, "verification_status": "draft"}
        self.store.save(1, draft)
        saved = self.store.latest(1)["payload"]
        self.assertEqual(saved["rounding_amount"], "")
        self.assertEqual(saved["payment_reference"], "00123")
        self.assertIsNone(self.store.handoff(1))
        with self.assertRaisesRegex(MailImportError, "Без ручного"):
            self.store.enqueue(1, None)
        invalid = copy.deepcopy(self.payload)
        invalid["allocations"][0]["object_id"] = None
        with self.assertRaises(MailImportError):
            self.store.approve(1, invalid, 1)

    def test_handoff_retry_after_crash_uses_same_identity_and_keeps_original(self):
        self.store.approve(1, self.payload, 0)
        identifier = self.store.handoff(1)["client_id"]
        queued = []

        def enqueue(client_id, *args, **kwargs):
            queued.append(client_id)
            if len(queued) == 1:
                raise OSError("simulated crash")

        host = SimpleNamespace(
            _store_expense_originals=lambda paths, client_id: paths,
            queue_expense_document_operation=enqueue,
        )
        with self.assertRaises(OSError):
            self.store.enqueue(1, host)
        self.store.close()
        self.store = DesktopMailStore(self.root, lambda: self.catalog)
        self.store.enqueue(1, host)
        self.store.enqueue(1, host)
        self.assertEqual(queued, [identifier, identifier])
        self.assertTrue(self.store.document(1)[1].is_file())

    def test_same_invoice_different_bytes_requires_review_not_second_handoff(self):
        self.store.approve(1, self.payload, 0)
        self.accept(b"different pdf of same invoice", 2)
        with self.assertRaisesRegex(MailImportError, "уже подтверждена"):
            self.store.approve(2, self.payload, 0)
        self.assertIsNone(self.store.handoff(2))

    def test_order_cannot_be_approved_with_invoice_payload(self):
        self.store.conn.execute(
            "UPDATE documents SET result_json=? WHERE id=1",
            ('{"fields":{"document_type":"order_confirmation","raw_text":"ORDERBEKRAFTELSE"}}',),
        )
        self.store.conn.commit()
        with self.assertRaises(MailImportError):
            self.store.approve(1, self.payload, 0)

    def test_old_reminder_invoice_label_is_rechecked_before_approval(self):
        import json
        fields = {**self.payload, "raw_text": "Nordea\nPaminnelse\nFaktura-/Avinummer\n123"}
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET result_json=? WHERE id=1",
                                    (json.dumps({"fields": fields}),))
        with self.assertRaises(MailImportError):
            self.store.approve(1, self.payload, 0)
        self.assertIsNone(self.store.handoff(1))
