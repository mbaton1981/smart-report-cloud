import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from expense_documents import ExtractionResult
from invoice_processing import recognize_invoice


class InvoiceProcessingTests(unittest.TestCase):
    def test_receipt_candidate_uses_local_ocr_when_heading_is_missing_from_text(self):
        text = "\n".join((
            "Date 2026-11-12", "Transaction ID ch_123456789",
            "Description Amount", "Copilot Max $100.00 USD",
            "Copilot Usage $14.37 USD", "Total $114.37 USD*",
        ))
        extraction = ExtractionResult(
            text=text, method="текст PDF", confidence=1.0,
            pages=(text,), page_elements=((),),
        )
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "receipt.pdf"
            source.write_bytes(b"isolated test PDF placeholder")
            with patch("expense_documents.extract_local_document", return_value=extraction), \
                    patch("expense_documents.ocr_pdf_heading", return_value="Receipt") as ocr:
                result = recognize_invoice([source])

        ocr.assert_called_once_with(source)
        self.assertEqual(result["fields"]["document_type"], "receipt")
        self.assertEqual(result["fields"]["gross_amount"], "114.37")
        self.assertIsNone(result["fields"]["vat_amount"])
        self.assertIn("локальный OCR заголовка", result["methods"])


if __name__ == "__main__":
    unittest.main()
