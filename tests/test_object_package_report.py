import hashlib
import importlib
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from PIL import Image

from object_package_report import (
    collect_object_expenses, expense_totals_by_currency, write_object_report_package,
)


def _document(identifier, *, status="confirmed", currency="SEK", date="2026-11-02",
              allocations=None, net="10.00", vat="2.00", gross="12.00"):
    return {
        "id": identifier,
        "client_id": f"client-{identifier}",
        "supplier_name": f"Supplier {identifier}",
        "invoice_number": f"INV-{identifier}",
        "document_date": date,
        "document_type": "invoice",
        "currency_code": currency,
        "net_amount": net,
        "vat_amount": vat,
        "gross_amount": gross,
        "verification_status": status,
        "has_original": bool(allocations),
        "attachments": [],
        "allocations": allocations or [],
    }


class ObjectPackageReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        windows_fonts = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
        regular = windows_fonts / "arial.ttf"
        bold = windows_fonts / "arialbd.ttf"
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont("ObjectPackageTestArial", str(regular)))
            pdfmetrics.registerFont(TTFont("ObjectPackageTestArialBold", str(bold)))
            cls.font, cls.bold_font = "ObjectPackageTestArial", "ObjectPackageTestArialBold"
        else:
            cls.font, cls.bold_font = "Helvetica", "Helvetica-Bold"

    def test_expense_projection_uses_only_confirmed_object_shares_once(self):
        source = _document(1, allocations=[
            {"object_id": 42, "destination": "object", "net_amount": "10.00",
             "vat_amount": "2.00", "gross_amount": "12.00", "rounding_amount": "0.00"},
            {"object_id": 42, "destination": "object", "net_amount": "5.00",
             "vat_amount": "1.00", "gross_amount": "6.00", "rounding_amount": "0.00"},
            {"object_id": 84, "destination": "object", "net_amount": "20.00",
             "vat_amount": "4.00", "gross_amount": "24.00", "rounding_amount": "0.00"},
        ])
        no_tax = _document(
            2, allocations=[
                {"object_id": 42, "destination": "object", "net_amount": None,
                 "vat_amount": None, "gross_amount": "8.00", "rounding_amount": None},
                {"object_id": 42, "destination": "object", "net_amount": None,
                 "vat_amount": None, "gross_amount": None, "rounding_amount": None},
            ], net=None, vat=None, gross="8.00",
        )
        no_tax["document_type"] = "manual"
        unconfirmed = _document(
            3, status="requires_review", allocations=[{
                "object_id": 42, "destination": "object", "net_amount": "4.00",
                "vat_amount": "1.00", "gross_amount": "5.00",
            }], net="4.00", vat="1.00", gross="5.00",
        )
        company_share = _document(4, allocations=[{
            "object_id": 42, "destination": "company", "company_expense": True,
            "net_amount": "7.00", "vat_amount": "1.00", "gross_amount": "8.00",
        }])

        included, excluded = collect_object_expenses(
            [source, no_tax, unconfirmed, company_share], 42,
        )
        self.assertEqual(len(included), 2)
        self.assertEqual(included[0]["share"]["net_amount"], Decimal("15.00"))
        self.assertEqual(included[0]["share"]["vat_amount"], Decimal("3.00"))
        self.assertEqual(included[0]["share"]["gross_amount"], Decimal("18.00"))
        self.assertEqual(included[0]["share"]["rounding_amount"], Decimal("0.00"))
        self.assertIsNone(included[1]["share"]["net_amount"])
        self.assertIsNone(included[1]["share"]["vat_amount"])
        self.assertIsNone(included[1]["share"]["gross_amount"])
        self.assertEqual(
            included[1]["share"]["known_amounts"]["gross_amount"], Decimal("8.00"),
        )
        self.assertEqual(len(excluded), 1)
        self.assertIn("не подтверждён", excluded[0]["excluded_reason"])

        totals = expense_totals_by_currency(included)
        self.assertIsNone(totals["SEK"]["gross_amount"])
        self.assertEqual(totals["SEK"]["known_amounts"]["gross_amount"], Decimal("26.00"))
        self.assertEqual(totals["SEK"]["known_amounts"]["net_amount"], Decimal("15.00"))
        self.assertIsNone(totals["SEK"]["net_amount"])
        self.assertEqual(set(totals), {"SEK"})

    def test_package_keeps_original_pages_and_separates_work_ob_and_trip_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "invoice.pdf"
            original = canvas.Canvas(str(source_pdf))
            for label in ("Original page one", "Original page two"):
                original.drawString(40, 740, label)
                original.showPage()
            original.save()
            source_image = root / "decision.png"
            Image.new("RGB", (80, 40), "white").save(source_image)
            original_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (source_pdf, source_image)
            }

            first = _document(10, allocations=[
                {"object_id": 42, "destination": "object", "net_amount": "10.00",
                 "vat_amount": "2.00", "gross_amount": "12.00", "rounding_amount": "0.00"},
                {"object_id": 42, "destination": "object", "net_amount": None,
                 "vat_amount": None, "gross_amount": None, "rounding_amount": None},
                {"object_id": 84, "destination": "object", "net_amount": "20.00",
                 "vat_amount": "4.00", "gross_amount": "24.00", "rounding_amount": "0.00"},
            ])
            second = _document(
                11, allocations=[{
                    "object_id": 42, "destination": "object", "net_amount": None,
                    "vat_amount": None, "gross_amount": "8.00",
                }], net=None, vat=None, gross="8.00",
            )
            second["document_type"] = "manual"
            missing = _document(12, currency="EUR", allocations=[{
                "object_id": 42, "destination": "object", "net_amount": None,
                "vat_amount": None, "gross_amount": "5.00",
            }], net=None, vat=None, gross="5.00")
            missing["has_original"] = False
            pending = _document(13, status="requires_review", allocations=[{
                "object_id": 42, "destination": "object", "gross_amount": "3.00",
            }], net=None, vat=None, gross="3.00")
            expenses, excluded = collect_object_expenses(
                [first, second, missing, pending], 42,
            )
            work_report = {
                "journal_rows": [
                    {"date": "2026-11-02", "employee": "Synthetic Worker", "comment": "Repair",
                     "hours": 8, "transport": None, "entry_type": "work", "sync_id": "work-1",
                     "hours_confirmed": 1, "invoiced": 0},
                    {"date": "2026-11-02", "employee": "Second Worker", "comment": "Install",
                     "hours": 6, "transport": 2, "entry_type": "work", "sync_id": "work-2",
                     "hours_confirmed": 1, "invoiced": 0},
                    {"date": "2026-11-02", "employee": "Synthetic Worker", "comment": "Evening",
                     "hours": 4, "transport": 0, "entry_type": "ob", "sync_id": "ob-1",
                     "hours_confirmed": 0, "invoiced": 0},
                    {"date": "2026-11-02", "employee": "Synthetic Worker", "comment": "Sick",
                     "hours": 3, "transport": 0, "entry_type": "sick", "sync_id": "sick-1"},
                ],
                "journal_segments": {
                    "ob-1": [{
                        "start_time": "2026-11-02 17:00", "end_time": "2026-11-02 19:00",
                        "hours": 2, "category": "weekday_early_evening",
                    }],
                },
                "prepared_rows": [{
                    "employee": "Synthetic Worker", "hours": 12, "without_ob_hours": 10,
                }, {
                    "employee": "Second Worker", "hours": 6, "without_ob_hours": 6,
                }],
                "cost_lines": [{
                    "label": "Arbete", "quantity": 16, "unit": "h", "rate": 500, "amount": 8000,
                }],
                "totals": {
                    "hours": 18, "without_ob_hours": 16, "ob_categories": {},
                    "known_subtotal": 8000, "invoice_total": 8000,
                },
                "warnings": [],
            }
            data = {
                "object": {
                    "name": "Badbacken", "markning": "p1022",
                    "company": "Renatur", "is_active": 0,
                },
                "period_label": "за весь объект; фактический диапазон данных 2026-11-02 — 2026-11-02",
                "generated_at": "2026-11-03 12:00",
                "customer_report": work_report,
                "include_customer_cost": True,
                "include_copies": True,
                "expenses": expenses,
                "excluded_expenses": excluded,
                "expense_snapshot_available": True,
                "incomplete_reasons": [],
            }
            target = root / "full-report.pdf"
            result = write_object_report_package(
                target, data, font=self.font, bold_font=self.bold_font, source_paths={
                    "client-10": [source_pdf],
                    "client-11": [source_image],
                },
            )
            reader = PdfReader(str(target))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)

            self.assertGreaterEqual(result["page_count"], 6)
            self.assertIn("Bygger och renoverar i Sthlm AB", text)
            self.assertIn("Архивный объект", text)
            self.assertIn("OB-часы (часть общего количества)", text)
            self.assertIn("18,0 ч", text)
            self.assertIn("16,0 ч", text)
            self.assertIn("10,0 ч", text)
            self.assertIn("2,0 ч", text)
            self.assertIn("Second Worker", text)
            self.assertIn("Поездки\nНеизвестно", text)
            self.assertIn("10,00 (известная часть; неполно)", text)
            self.assertIn("20,00 SEK (известная часть; итог неполон)", text)
            self.assertIn("Поездки", text)
            self.assertIn("Часы подтверждены", text)
            self.assertIn("Приложение 1", text)
            self.assertIn("Копия отсутствует или неполна", text)
            self.assertIn("Original page one", text)
            self.assertIn("Original page two", text)
            self.assertIn("не включённые", text.casefold())
            self.assertNotIn("Sick", text)
            self.assertNotIn("зарплата", text.casefold())
            self.assertEqual(len(reader.outline), 3)
            for path, digest in original_hashes.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            self.assertTrue(result["incomplete_reasons"])

    def test_unknown_date_is_excluded_from_period_without_assuming_zero(self):
        document = _document(
            20, date="", allocations=[{
                "object_id": 42, "destination": "object", "net_amount": None,
                "vat_amount": None, "gross_amount": "8.00",
            }], net=None, vat=None, gross="8.00",
        )
        inside = _document(21, date="2026-11-30", allocations=[{
            "object_id": 42, "destination": "object", "gross_amount": "5.00",
        }], net=None, vat=None, gross="5.00")
        outside = _document(22, date="2026-10-31", allocations=[{
            "object_id": 42, "destination": "object", "gross_amount": "3.00",
        }], net=None, vat=None, gross="3.00")
        included, excluded = collect_object_expenses(
            [document, inside, outside], 42, "2026-11-01", "2026-11-30",
        )
        self.assertEqual([item["id"] for item in included], [21])
        self.assertEqual(len(excluded), 1)
        self.assertIn("Дата неизвестна", excluded[0]["excluded_reason"])
        self.assertEqual(collect_object_expenses([], 42, "2026-11-01", "2026-11-30"), ([], []))


class ExpensePaginationTests(unittest.TestCase):
    def test_client_fetches_all_pages_and_rejects_changing_snapshot(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            app = importlib.import_module("app")
        pages = [
            {"documents": [{"id": 1}, {"id": 2}], "total": 3, "offset": 0,
             "limit": 2, "has_more": True},
            {"documents": [{"id": 3}], "total": 3, "offset": 2,
             "limit": 2, "has_more": False},
        ]
        with patch.object(app, "EXPENSE_DOCUMENT_PAGE_SIZE", 2), \
                patch.object(app, "request_expense_api", side_effect=pages):
            documents = app.fetch_all_expense_documents("2026-11-01", "2026-11-30")
        self.assertEqual([document["id"] for document in documents], [1, 2, 3])

        changed_pages = [
            pages[0],
            {"documents": [{"id": 3}], "total": 4, "offset": 2,
             "limit": 2, "has_more": True},
        ]
        with patch.object(app, "EXPENSE_DOCUMENT_PAGE_SIZE", 2), \
                patch.object(app, "request_expense_api", side_effect=changed_pages):
            with self.assertRaisesRegex(RuntimeError, "изменился"):
                app.fetch_all_expense_documents()

    def test_profile_originals_are_rejected_when_sha256_does_not_match(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            app = importlib.import_module("app")
        with tempfile.TemporaryDirectory() as directory:
            local_copy = Path(directory) / "copy.pdf"
            local_copy.write_bytes(b"not the registered original")
            document = {
                "client_id": "client-profile",
                "local_attachment_paths": [str(local_copy)],
                "attachments": [{"sha256": "0" * 64}],
            }
            sources, errors = app.SmartReportApp._download_object_report_originals(
                SimpleNamespace(), [document], directory, lambda _message: None,
            )
        self.assertEqual(sources["client-profile"], [])
        self.assertIn("SHA-256", errors["client-profile"])

    def test_work_history_marks_only_unsynced_shifts_in_selected_period(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            app = importlib.import_module("app")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE objects (id INTEGER, sync_id TEXT, company TEXT, name TEXT, markning TEXT);
            CREATE TABLE shifts (sync_id TEXT, company TEXT, object_name TEXT, entry_type TEXT, date TEXT);
            CREATE TABLE shift_sync_outbox (sync_id TEXT, state TEXT);
            INSERT INTO objects VALUES (42, 'object-42', 'Renatur', 'Badbacken', 'p1022');
            INSERT INTO shifts VALUES ('shift-1', 'Renatur', 'Badbacken', 'work', '2026-11-02');
            INSERT INTO shifts VALUES ('shift-2', 'Renatur', 'Badbacken', 'ob', '2026-10-25');
            INSERT INTO shift_sync_outbox VALUES ('shift-1', 'pending');
            INSERT INTO shift_sync_outbox VALUES ('shift-1', 'conflict');
            INSERT INTO shift_sync_outbox VALUES ('shift-2', 'blocked');
        """)
        connection.commit()
        with patch.object(app, "get_db_connection", return_value=connection), \
                patch.object(app, "invoice_object_names", return_value=["Badbacken"]):
            result = app.SmartReportApp._object_work_history_range(
                SimpleNamespace(), {
                    "id": 42, "sync_id": "object-42",
                }, "2026-11-01", "2026-11-30",
            )
        self.assertEqual(result[0], ("2026-10-25", "2026-11-02"))
        self.assertEqual(result[1], 1)
        self.assertEqual(set(result[2].split(",")), {"pending", "conflict"})

