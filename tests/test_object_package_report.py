import hashlib
import importlib
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from PIL import Image

from object_package_report import (
    _expense_description, collect_object_expenses, expense_totals_by_currency,
    write_object_report_package,
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
            self.assertIn("Работа и поездки вместе", text)
            self.assertIn("18,0 ч", text)
            self.assertIn("8,0 ч", text)
            self.assertIn("6,0 ч", text)
            self.assertIn("2,0 ч", text)
            self.assertIn("Second Worker", text)
            self.assertIn("Сумма поездок: Неизвестно", text)
            self.assertIn("12,00 SEK (известная часть; неполно)", text)
            self.assertIn("20,00 SEK (известная часть; итог неполон)", text)
            self.assertIn("5,00 EUR", text)
            self.assertNotIn("EUR: 5,00 EUR", text)
            self.assertIn("2026-11-02", text)
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

    def test_swedish_cover_shows_object_shares_travel_rates_and_source_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = {}
            source_paths = {}
            originals_by_invoice = {
                "client-30": [
                    ("order 8782530-01", "Sälj.ordnr: 8782530-01", "Proj. nr: P1040"),
                ],
                "client-31": [
                    ("order 8781867-01", "Sälj.ordnr: 8781867-01", "Proj. nr: P1040"),
                    ("order detail", "Sälj.ordnr: 8781867-01", "Proj. nr: P1040"),
                    ("terms", "Terms and conditions", ""),
                ],
                "client-32": [
                    ("order 8779738-01", "Sälj.ordnr: 8779738-01", "Proj. nr: P1040"),
                    ("order detail", "Sälj.ordnr: 8779738-01", "Proj. nr: P1040"),
                ],
            }
            for key, pages in originals_by_invoice.items():
                source = root / f"{key}.pdf"
                output = canvas.Canvas(str(source))
                for label, order, project in pages:
                    output.drawString(40, 740, label)
                    output.drawString(40, 720, order)
                    if project:
                        output.drawString(40, 700, project)
                    output.showPage()
                output.save()
                originals[source] = hashlib.sha256(source.read_bytes()).hexdigest()
                source_paths[key] = [source]

            first = _document(30, allocations=[
                {"object_id": 42, "destination": "object", "net_amount": "641.92",
                 "vat_amount": "160.48", "gross_amount": "802.45",
                 "rounding_amount": "0.05"},
                {"object_id": 84, "destination": "object", "net_amount": "2992.00",
                 "vat_amount": "748.00", "gross_amount": "3740.55",
                 "rounding_amount": "0.00"},
            ], gross="4543.00")
            second = _document(31, allocations=[
                {"object_id": 42, "destination": "object", "net_amount": None,
                 "vat_amount": None, "gross_amount": "3489.34",
                 "rounding_amount": None},
                {"object_id": 84, "destination": "object", "net_amount": "939.73",
                 "vat_amount": "234.93", "gross_amount": "1174.66",
                 "rounding_amount": "0.00"},
            ], net=None, vat=None, gross="4664.00")
            third = _document(32, allocations=[
                {"object_id": 42, "destination": "object", "net_amount": "9167.88",
                 "vat_amount": "2291.99", "gross_amount": "11459.87",
                 "rounding_amount": "0.00"},
                {"object_id": 84, "destination": "object", "net_amount": "100000.00",
                 "vat_amount": "25000.00", "gross_amount": "128169.90",
                 "rounding_amount": "0.23"},
            ], net="109167.88", vat="27291.99", gross="139629.77")
            third["allocation_basis"] = "net"
            third["amount_due"] = "139630.00"
            third["rounding_amount"] = "0.23"
            for document in (first, second, third):
                document["expense_category"] = "Материалы"
            expenses, excluded = collect_object_expenses([first, second, third], 42)

            report = {
                "journal_rows": [
                    {"date": "2026-11-02", "employee": "Worker", "comment": "",
                     "hours": 1, "transport": 2, "entry_type": "work", "sync_id": "w1",
                     "hours_confirmed": 1, "invoiced": 0},
                    {"date": "2026-11-03", "employee": "Worker", "comment": "",
                     "hours": 1, "transport": 1, "entry_type": "work", "sync_id": "w2",
                     "hours_confirmed": 1, "invoiced": 0},
                ],
                "journal_segments": {},
                "prepared_rows": [],
                "cost_lines": [
                    {"label": "Arbete", "quantity": 2, "unit": "h",
                     "rate": 480, "amount": 960},
                    {"label": "Resa", "quantity": 2, "unit": "st",
                     "rate": 450, "amount": 900},
                    {"label": "Resa", "quantity": 1, "unit": "st",
                     "rate": 500, "amount": 500},
                ],
                "totals": {
                    "work_amount": 960, "ob_amount": 0, "transport_amount": 1400,
                    "known_subtotal": 2360, "invoice_total": 2360,
                },
                "warnings": [],
            }
            data = {
                "language": "sv",
                "object": {"name": "Storholmenvagen 42 Staffan Bathroom",
                           "markning": "p1040", "company": "Staffan", "is_active": 1},
                "customer_name": "Staffan",
                "period_label": "2026-11-02 — 2026-11-03",
                "generated_at": "2026-11-15 12:00",
                "customer_report": report,
                "journal_translations": {},
                "include_customer_cost": True,
                "include_copies": True,
                "expenses": expenses,
                "excluded_expenses": excluded,
                "expense_snapshot_available": True,
                "incomplete_reasons": [],
            }
            target = root / "p1040-report.pdf"
            write_object_report_package(
                target, data, font=self.font, bold_font=self.bold_font,
                source_paths=source_paths,
            )
            reader = PdfReader(str(target))
            first_page = reader.pages[0].extract_text()
            journal_page = reader.pages[1].extract_text()
            text = "\n".join(page.extract_text() or "" for page in reader.pages)

            self.assertIn("Bygger och renoverar i Sthlm AB", first_page)
            self.assertIn("Arbete", first_page)
            self.assertIn("960,00 SEK", first_page)
            self.assertNotIn("SEK: 15 751,66 SEK", first_page)
            self.assertIn("Totalt resor: 3 st", first_page)
            self.assertIn("Summa resor: 1 400,00 SEK exkl. moms", first_page)
            normalized_first_page = " ".join(first_page.split())
            self.assertIn("2 st × 450,00 SEK", normalized_first_page)
            self.assertIn("1 st × 500,00 SEK", normalized_first_page)
            self.assertIn("Arbetsjournal", journal_page)
            self.assertNotIn("Arbetsjournal", first_page)
            self.assertIn("2026-11-02", journal_page)
            self.assertIn("15 751,66 SEK", text)
            self.assertIn("Känd nettodel: 9 809,80 SEK", text)
            self.assertIn("inte en fullständig nettosumma", text)
            self.assertIn("På detta objekt: 3 489,34 SEK", text)
            self.assertIn("På detta objekt: 11 459,87 SEK", text)
            self.assertIn("Leverantörens fullständiga fakturabelopp: 4 664,00 SEK", text)
            self.assertIn("Leverantörens fullständiga fakturabelopp: 139 630,00 SEK", text)
            self.assertIn("Fakturan omfattar inköp för flera objekt", text)
            self.assertIn("original 1 s. 1, 2", text)
            self.assertNotRegex(text, r"[\u0400-\u04ff]")
            for path, digest in originals.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            for pages in originals_by_invoice.values():
                for label, *_ in pages:
                    self.assertIn(label, text)

    def test_materials_without_reliable_item_description_use_neutral_fallback(self):
        self.assertEqual(
            _expense_description({"expense_category": "Материалы"}, "p1040"),
            "Objektets fördelade kostnad; artikelspecifikation saknas.",
        )

    def test_net_basis_supplier_total_stays_unknown_without_due_or_rounding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "invoice.pdf"
            output = canvas.Canvas(str(source))
            output.drawString(40, 740, "Invoice")
            output.save()

            document = _document(33, allocations=[{
                "object_id": 42, "destination": "object", "net_amount": "80.00",
                "vat_amount": "20.00", "gross_amount": "100.00",
            }], net="80.00", vat="20.00", gross="100.00")
            document.update(
                allocation_basis="net", amount_due=None, rounding_amount=None,
                expense_category="Материалы",
            )
            expenses, excluded = collect_object_expenses([document], 42)
            data = {
                "language": "sv",
                "object": {"name": "Testobjekt", "markning": "p1040",
                           "company": "Staffan", "is_active": 1},
                "customer_name": "Staffan",
                "period_label": "2026-11-02 — 2026-11-02",
                "generated_at": "2026-11-15 12:00",
                "customer_report": {
                    "journal_rows": [], "journal_segments": {}, "cost_lines": [],
                    "totals": {
                        "work_amount": 0, "ob_amount": 0, "transport_amount": 0,
                        "known_subtotal": 0, "invoice_total": 0,
                    },
                    "warnings": [],
                },
                "journal_translations": {},
                "include_customer_cost": False,
                "include_copies": True,
                "expenses": expenses,
                "excluded_expenses": excluded,
                "expense_snapshot_available": True,
                "incomplete_reasons": [],
            }
            target = root / "unknown-invoice-total.pdf"
            write_object_report_package(
                target, data, font=self.font, bold_font=self.bold_font,
                source_paths={"client-33": [source]},
            )
            text = "\n".join(
                page.extract_text() or "" for page in PdfReader(str(target)).pages
            )

        self.assertIn("På detta objekt: 100,00 SEK", text)
        self.assertIn("Leverantörens fullständiga fakturabelopp: Okänt", text)

    def test_long_object_names_and_journal_rows_flow_across_pages(self):
        long_name = (
            "Storholmenvagen 42 Staffan Bathroom renovation and plumbing replacement "
            "for the north-facing apartment and shared utility room"
        )
        rows = [
            {
                "date": "2026-11-02", "employee": f"Worker {index:02d}", "comment": "",
                "hours": 1, "transport": 0, "entry_type": "work",
                "sync_id": f"shift-{index:02d}", "hours_confirmed": 1, "invoiced": 0,
            }
            for index in range(45)
        ]
        data = {
            "language": "sv",
            "object": {"name": long_name, "markning": "p1040",
                       "company": "Staffan", "is_active": 1},
            "customer_name": "Staffan",
            "period_label": "2026-11-02 — 2026-11-02",
            "generated_at": "2026-11-15 12:00",
            "customer_report": {
                "journal_rows": rows,
                "journal_segments": {},
                "cost_lines": [],
                "totals": {
                    "work_amount": 21600, "ob_amount": 0, "transport_amount": 0,
                    "known_subtotal": 21600, "invoice_total": 21600,
                },
                "warnings": [],
            },
            "journal_translations": {
                f"shift-{index:02d}": (
                    f"Renovering av badrum och installation av rördel "
                    f"för arbetsmoment {index:02d}"
                )
                for index in range(45)
            },
            "include_customer_cost": False,
            "include_copies": False,
            "expenses": [],
            "expense_snapshot_available": True,
            "incomplete_reasons": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "long-report.pdf"
            result = write_object_report_package(
                target, data, font=self.font, bold_font=self.bold_font,
            )
            reader = PdfReader(str(target))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n".join(pages)

        self.assertGreaterEqual(result["page_count"], 4)
        self.assertIn(long_name, " ".join(pages[0].split()))
        self.assertIn("Arbetsjournal", pages[1])
        self.assertIn("Worker 44", text)
        self.assertGreaterEqual(text.count("Arbetsbeskrivning"), 2)

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

    def test_swedish_draft_uses_translations_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "invoice.pdf"
            original = canvas.Canvas(str(source))
            for label in ("Original page one", "Original page two"):
                original.drawString(40, 740, label)
                original.showPage()
            original.save()
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            document = _document(10, allocations=[{
                "object_id": 42, "destination": "object", "net_amount": "10.00",
                "vat_amount": "2.00", "gross_amount": "12.00", "rounding_amount": "0.00",
            }])
            document["document_type"] = "manual"
            document["expense_category"] = "Прочие расходы"
            expenses, excluded = collect_object_expenses([document], 42)
            data = {
                "language": "sv",
                "object": {"name": "Badbacken", "markning": "p1022",
                           "company": "Privat", "is_active": 1},
                "period_label": "за весь объект; фактический диапазон данных 2026-11-02 — 2026-11-02",
                "generated_at": "2026-11-03 12:00",
                "customer_report": {
                    "journal_rows": [
                        {"date": "2026-11-02", "employee": "Worker", "comment": "Монтаж",
                         "hours": 8, "transport": 0, "entry_type": "work", "sync_id": "one",
                         "hours_confirmed": 0, "invoiced": 0},
                        {"date": "2026-11-02", "employee": "Worker", "comment": "",
                         "hours": 2, "transport": 0, "entry_type": "work", "sync_id": "two",
                         "hours_confirmed": 1, "invoiced": 0},
                        {"date": "2026-11-02", "employee": "Worker", "comment": "Неясное",
                         "hours": 1, "transport": 0, "entry_type": "work", "sync_id": "three",
                         "hours_confirmed": 0, "invoiced": 0},
                    ],
                    "journal_segments": {},
                    "prepared_rows": [{"employee": "Worker", "without_ob_hours": 11}],
                    "cost_lines": [],
                    "totals": {"without_ob_hours": 11, "invoice_total": 0},
                    "warnings": ["Число поездок неизвестно; расчёт клиенту неполный."],
                },
                "journal_translations": {"one": "Montering"},
                "include_customer_cost": True, "include_copies": True,
                "expenses": expenses, "excluded_expenses": excluded,
                "expense_snapshot_available": True,
                "incomplete_reasons": [
                    "Не включено в расходы документов, требующих проверки или подтверждения: 2.",
                    "Число поездок в части рабочих записей неизвестно.",
                ],
            }
            target = root / "draft.pdf"
            result = write_object_report_package(
                target, data, font=self.font, bold_font=self.bold_font,
                source_paths={"client-10": [source]},
            )
            reader = PdfReader(str(target))
            generated_text = "\n".join(
                page.extract_text() or "" for page in reader.pages[:-2]
            )
            self.assertIn("UTKAST", generated_text)
            self.assertNotIn("Beställare:", generated_text)
            self.assertIn("Montering", generated_text)
            self.assertIn("Arbetsbeskrivning saknas", generated_text)
            self.assertIn(
                "Arbetsbeskrivning behöver förtydligas",
                " ".join(generated_text.split()),
            )
            self.assertIn("Ej bekräftad", generated_text)
            self.assertIn("Antal arbetsposter med obekräftade timmar: 2", generated_text)
            self.assertIn("Manuellt registrerat dokument", generated_text)
            self.assertIn("Dokument som behöver granskas", generated_text)
            self.assertNotIn("manual", generated_text)
            self.assertNotRegex(generated_text, r"[\u0400-\u04ff]")
            self.assertEqual(result["page_count"], len(reader.pages))
            self.assertIn("Original page one", reader.pages[-2].extract_text())
            self.assertIn("Original page two", reader.pages[-1].extract_text())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)

    def test_swedish_customer_name_is_optional_without_forcing_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            data = {
                "language": "sv", "object": {"name": "Objekt", "company": "Privat"},
                "generated_at": "2026-11-03 12:00", "period_label": "2026-11-02",
                "customer_report": {
                    "journal_rows": [{
                        "date": "2026-11-02", "employee": "Worker", "comment": "Монтаж",
                        "hours": 8, "transport": 0, "entry_type": "work",
                        "sync_id": "one", "hours_confirmed": 1, "invoiced": 0,
                    }],
                    "journal_segments": {},
                    "prepared_rows": [{"employee": "Worker", "without_ob_hours": 8}],
                    "totals": {"without_ob_hours": 8}, "warnings": [],
                },
                "journal_translations": {"one": "Montering"},
                "include_customer_cost": False, "include_copies": False,
                "expenses": [], "expense_snapshot_available": True, "incomplete_reasons": [],
            }
            target = Path(directory) / "report.pdf"
            result = write_object_report_package(
                target, data, font=self.font, bold_font=self.bold_font,
            )
            text = "\n".join(page.extract_text() or "" for page in PdfReader(target).pages)
            self.assertNotIn("Beställare:", text)
            self.assertNotIn("UTKAST", text)
            self.assertIn("0,00 SEK", text)
            self.assertIn("Inga bekräftade andelar", text)
            self.assertEqual(result["incomplete_reasons"], [])
            self.assertIn("Montering", text)

    def test_object_card_report_task_builds_pdf_with_work_rows_and_unknown_trips(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            app = importlib.import_module("app")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "original.pdf"
            original = canvas.Canvas(str(source_pdf))
            original.drawString(40, 740, "Original invoice page")
            original.save()
            document = _document(10, allocations=[
                {"object_id": 42, "destination": "object", "net_amount": "10.00",
                 "vat_amount": "2.00", "gross_amount": "12.00"},
                {"object_id": 84, "destination": "object", "net_amount": "20.00",
                 "vat_amount": "4.00", "gross_amount": "24.00"},
            ])
            object_row = {
                "id": 42, "server_id": 42, "sync_id": "object-42",
                "name": "Badbacken", "markning": "p1022", "company": "Test",
            }
            options = {
                "all_period": True, "date_from": None, "date_to": None,
                "include_copies": True, "include_customer_cost": True,
            }
            for transport in (None, 1):
                with self.subTest(transport=transport):
                    work_report = {
                        "journal_rows": [
                            {"date": "2026-11-02", "employee": "Worker", "comment": "Work",
                             "hours": 8, "transport": transport, "entry_type": "work",
                             "sync_id": "shift-1", "hours_confirmed": 1, "invoiced": 0},
                            {"date": "2026-11-02", "employee": "Worker", "comment": "Sick",
                             "hours": 2, "transport": None, "entry_type": "sick"},
                        ],
                        "journal_segments": {}, "prepared_rows": [], "cost_lines": [],
                        "totals": {"hours": 8, "without_ob_hours": 8, "ob_categories": {},
                                   "known_subtotal": 4000, "invoice_total": 4000},
                        "warnings": [],
                    }
                    completions = []
                    controller = SimpleNamespace(
                        object_full_report_button=Mock(),
                        statusBar=lambda: SimpleNamespace(showMessage=Mock()),
                        _on_full_object_report_progress=lambda *_args: None,
                        _on_full_object_report_ready=lambda *args: completions.append(args),
                        _object_work_history_range=lambda *_args: (
                            ("2026-11-02", "2026-11-02"), 0, ""
                        ),
                        _expense_document_snapshot=lambda *_args, **_kwargs: ([document], True),
                        collect_invoice_report=lambda **_kwargs: work_report,
                        _prepare_invoice_view=lambda report: report,
                        _download_object_report_originals=lambda *_args: (
                            {"client-10": [source_pdf]}, {}
                        ),
                    )
                    controller._reserve_object_report_path = lambda *args: (
                        app.SmartReportApp._reserve_object_report_path(controller, *args)
                    )
                    controller._build_full_object_package = lambda *args: (
                        app.SmartReportApp._build_full_object_package(controller, *args)
                    )
                    controller._finish_full_object_package = lambda *args: (
                        app.SmartReportApp._finish_full_object_package(controller, *args)
                    )
                    pool = SimpleNamespace(start=lambda task: task.run())
                    with patch.object(app, "fetch_all_expense_documents", return_value=[document]), \
                            patch.object(app, "REPORTS_DIR", str(root)), \
                            patch.object(app, "DEFAULT_FONT", self.font), \
                            patch.object(app, "DEFAULT_FONT_BOLD", self.bold_font), \
                            patch.object(app.QThreadPool, "globalInstance", return_value=pool):
                        app.SmartReportApp._start_full_object_report(controller, object_row, options)

                    self.assertEqual(len(completions), 1)
                    _, result, error = completions[0]
                    self.assertEqual(error, "")
                    text = "\n".join(
                        page.extract_text() or "" for page in PdfReader(result["path"]).pages
                    )
                    self.assertIn("Badbacken", text)
                    self.assertIn("8,0 ч", text)
                    self.assertIn("12,00", text)
                    self.assertIn("Original invoice page", text)
                    self.assertNotIn("Sick", text)
                    self.assertEqual(work_report["totals"]["invoice_total"] is None, transport is None)
                    self.assertEqual(
                        any("поездок" in reason.lower() for reason in result["incomplete_reasons"]),
                        transport is None,
                    )
                    if transport == 1:
                        swedish_options = dict(
                            options, language="sv", customer_name="Test",
                            review_note="Kontrollera avrundningen.",
                        )
                        with patch.object(app, "fetch_all_expense_documents", return_value=[document]):
                            review = controller._build_full_object_package(
                                object_row, swedish_options, lambda _message: None,
                            )
                        self.assertNotIn("path", review)
                        controller.table_objects = Mock()
                        controller.table_objects.currentRow.return_value = 0
                        controller.table_objects.item.return_value.data.return_value = 42
                        controller._object_package_request_id = 7
                        dialog = Mock()
                        dialog.exec.return_value = app.QDialog.DialogCode.Accepted
                        dialog.translations = {"shift-1": "Arbete"}
                        completions.clear()
                        with patch.object(app, "JournalTranslationDialog", return_value=dialog), \
                                patch.object(app, "REPORTS_DIR", str(root)), \
                                patch.object(app, "DEFAULT_FONT", self.font), \
                                patch.object(app, "DEFAULT_FONT_BOLD", self.bold_font), \
                                patch.object(app.QThreadPool, "globalInstance", return_value=pool):
                            app.SmartReportApp._on_full_object_report_ready(controller, 7, review, "")
                        self.assertEqual(len(completions), 1)
                        _, swedish_result, error = completions[0]
                        self.assertEqual(error, "")
                        swedish_text = "\n".join(
                            page.extract_text() or ""
                            for page in PdfReader(swedish_result["path"]).pages
                        )
                        self.assertIn("Beställare: Test", swedish_text)
                        self.assertIn("Arbete", swedish_text)
                        self.assertIn("Kontrollera avrundningen.", swedish_text)
                        self.assertIn("UTKAST", swedish_text)
                        self.assertIn("Summa resor: Okänt", swedish_text)
                        self.assertIn("Original invoice page", swedish_text)


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

