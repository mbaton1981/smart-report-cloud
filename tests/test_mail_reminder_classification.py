import unittest

from mail_import import classify_mail_document, recognition_status


class ReminderClassificationTests(unittest.TestCase):
    def test_reminder_header_overrides_default_invoice_without_inventing_fields(self):
        for title in ("Påminnelse", "Paminnelse", "Betalningspåminnelse", "Payment reminder"):
            with self.subTest(title=title):
                fields = {
                    "document_type": "invoice", "raw_text": (
                        f"Nordea\n{title}\nUtskriftsdatum\n2026-10-11\n"
                        "Faktura-/Avinummer\n19778426\nTotaltattbetala\n2.482.00"),
                    "supplier_name": None, "invoice_number": None, "document_date": None,
                    "net_amount": None, "vat_amount": None, "amount_due": None,
                    "payment_reference": "1977842606",
                }
                classify_mail_document(fields)
                self.assertEqual(fields["document_type"], "unclassified")
                self.assertEqual(fields["suggested_document_type"], "payment_reminder")
                self.assertEqual(recognition_status({"fields": fields}, "requires_review")["outcome"],
                                 "unclassified")
                for key in ("supplier_name", "invoice_number", "document_date",
                            "net_amount", "vat_amount", "amount_due"):
                    self.assertIsNone(fields[key])
                self.assertEqual(fields["payment_reference"], "1977842606")
                self.assertIn("Напоминание об оплате", fields["review_reasons"][0])

    def test_invoice_with_reminder_terms_is_not_a_reminder(self):
        fields = {"document_type": "invoice", "raw_text":
                  "Faktura Nr: 3539\nVid sen betalning tillkommer en påminnelseavgift.",
                  "invoice_number": "3539"}
        classify_mail_document(fields)
        self.assertEqual(fields["document_type"], "invoice")

    def test_receipt_does_not_become_invoice_or_gain_a_payable(self):
        fields = {"document_type": "receipt",
                  "raw_text": "Följesedel / Kvitto\nOrdernummer SO101598407\nBetalmetod Kortbetalning",
                  "invoice_number": None, "amount_due": None, "rounding_amount": None}
        classify_mail_document(fields)
        self.assertEqual(fields["document_type"], "receipt")
        self.assertIsNone(fields["invoice_number"])
        self.assertIsNone(fields["amount_due"])
        self.assertIsNone(fields["rounding_amount"])
