import importlib
from io import BytesIO
import os
import sqlite3
import unittest
from unittest.mock import patch

from pypdf import PdfReader
from customer_report import write_customer_pdf


class CustomerReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            cls.desktop = importlib.import_module("app")
            cls.application = cls.desktop.QApplication.instance() or cls.desktop.QApplication([])

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE objects(id INTEGER PRIMARY KEY, company TEXT, name TEXT, markning TEXT,
                rate REAL, transport_rate REAL, sync_id TEXT);
            CREATE TABLE companies(name TEXT);
            INSERT INTO companies VALUES ('Renatur');
            INSERT INTO objects VALUES(39,'Renatur','Tomteboda ÄTA','p1034',520,450,'object-t');
            INSERT INTO objects VALUES(84,'Renatur','HOTEL Djurgården','p1039',520,450,'object-h');
            CREATE TABLE employees(name TEXT, salary_rate REAL);
            INSERT INTO employees VALUES ('Andrii Piskun',245.8);
            CREATE TABLE shifts(id INTEGER PRIMARY KEY, date TEXT, employee TEXT, company TEXT,
                object_name TEXT, hours REAL, rate REAL DEFAULT 0, transport REAL DEFAULT 0,
                invoiced INTEGER DEFAULT 0, entry_type TEXT DEFAULT 'work', sync_id TEXT,
                start_time TEXT, end_time TEXT, work_rate_cache REAL DEFAULT 0, comment TEXT);
            CREATE TABLE ob_segments(id INTEGER PRIMARY KEY,shift_id TEXT,segment_date TEXT,
                start_time TEXT,end_time TEXT,hours REAL,category TEXT,premium_percent REAL,
                multiplier REAL,base_rate REAL,calculated_rate REAL,amount REAL,
                created_at TEXT,updated_at TEXT);
        """)
        self.conn.commit()

        def connection():
            cloned = sqlite3.connect(":memory:")
            self.conn.backup(cloned)
            cloned.row_factory = sqlite3.Row
            cloned.execute("PRAGMA query_only=ON")
            return cloned

        self.patcher = patch.object(self.desktop, "get_db_connection", connection)
        self.patcher.start()
        self.window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(self.window)
        self.window.tab_invoice = self.desktop.QWidget()
        self.window.tab_dagbok = self.desktop.QWidget()
        self.window.setup_invoice_tab()
        self.window.setup_dagbok_tab()
        self.window.inv_comp_cb.setCurrentText("Renatur")
        self.window.inv_obj_cb.setCurrentIndex(self.window.inv_obj_cb.findData(39))
        self.window.inv_date_from.setDate(self.desktop.QDate(2026, 10, 1))
        self.window.inv_date_to.setDate(self.desktop.QDate(2026, 10, 31))

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.patcher.stop()
        self.conn.close()

    def shift(self, name="Andrii Piskun", day="2026-10-21", hours=8, rate=0,
              transport=0, kind="work", object_name="Tomteboda ÄTA", comment="Montering"):
        identifier = str(self.conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] + 1)
        self.conn.execute("""
            INSERT INTO shifts(date,employee,company,object_name,hours,rate,transport,
                entry_type,sync_id,comment)
            VALUES(?,?,'Renatur',?,?,?,?,?,?,?)
        """, (day, name, object_name, hours, rate, transport, kind, identifier, comment))
        self.conn.commit()
        return identifier

    def ob(self, name="Andrii Piskun", day="2026-10-21", transport=1):
        identifier = self.shift(name, day, 4, transport=transport, kind="ob")
        result = self.desktop.calculate_work_interval(day, "16:00", "20:00", 245.8)
        self.desktop.replace_ob_segments(self.conn.cursor(), identifier, result["segments"])
        self.conn.execute("UPDATE shifts SET start_time='16:00',end_time='20:00' WHERE sync_id=?", (identifier,))
        self.conn.commit()
        return identifier

    def pdf(self, report, **options):
        output = BytesIO()
        write_customer_pdf(
            report, output, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD,
            self.window._journal_description_swedish, **options,
        )
        return PdfReader(output)

    def test_existing_ob_shape_objects_transport_and_pdf_separation(self):
        self.ob()
        self.shift(object_name="HOTEL Djurgården", transport=1)
        before = list(self.conn.iterdump())
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 4)
        self.assertEqual(report["totals"]["without_ob_hours"], 2)
        self.assertEqual(report["totals"]["ob_categories"], {"weekday_early_evening": 2})
        self.assertEqual(report["totals"]["known_subtotal"], 2530)
        self.assertIsNone(report["totals"]["invoice_total"])
        self.assertEqual(report["recorded_trips"], 1)
        pdf = self.pdf(report)
        self.assertEqual(len(pdf.pages), 2)
        first, journal = [page.extract_text() for page in pdf.pages]
        self.assertIn("Fakturaunderlag", first)
        self.assertIn("Preliminärt totalbelopp", first)
        self.assertIn("2 530,00", first)
        self.assertIn("utan ej fastställda", first)
        self.assertNotIn("Arbetsjournal", first)
        self.assertIn("Arbetsjournal", journal)
        self.assertIn("Montering", journal)
        self.assertNotIn("SEK", journal)
        self.assertNotIn("245", journal)
        for forbidden in ("Löneunderlag", "1 229", "983", "Начисления", "HOTEL"):
            self.assertNotIn(forbidden, first + journal)
        self.assertEqual(self.window.table_dagbok.rowCount(), 1)
        self.assertIn("4.0", self.window.dag_total_hours_lbl.text())
        self.window.inv_obj_cb.setCurrentIndex(self.window.inv_obj_cb.findData(84))
        hotel = self.window.generate_invoice()
        self.assertEqual(hotel["totals"]["hours"], 8)
        self.assertEqual(hotel["totals"]["invoice_total"], 4610)
        self.assertEqual(hotel["totals"]["hours"] + report["totals"]["hours"], 12)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_week_ten_employees_summary_and_journal_match(self):
        for day in range(5, 12):
            for employee in range(10):
                self.shift(f"Medarbetare {employee:02}", f"2026-10-{day:02}", 8,
                           transport=1 if employee == 0 else 0)
        report = self.window.generate_invoice()
        self.assertEqual(len(report["prepared_rows"]), 10)
        self.assertEqual(len(report["journal_rows"]), 70)
        self.assertEqual(report["totals"]["hours"], 560)
        self.assertEqual(sum(row["hours"] for row in report["journal_rows"]), 560)
        self.assertEqual(report["recorded_trips"], 7)
        self.assertEqual(report["totals"]["transport_amount"], 3150)
        self.assertEqual(report["totals"]["invoice_total"], 294350)
        pdf = self.pdf(report)
        texts = [page.extract_text() for page in pdf.pages]
        self.assertLessEqual(len(texts), 5)
        for employee in range(10):
            self.assertEqual(texts[0].count(f"Medarbetare {employee:02}"), 1)
            self.assertEqual("\n".join(texts[1:]).count(f"Medarbetare {employee:02}"), 7)
        for text in texts[1:]:
            self.assertIn("Datum", text)
            self.assertIn("Arbetsbeskrivning", text)
            self.assertNotIn("SEK", text)
        self.assertIn("Totalt belopp", texts[0])
        self.assertNotIn("PRELIMINÄRT", texts[0])
        self.assertEqual(self.window.table_invoice.rowCount(), 10)
        self.assertEqual(self.window.table_dagbok.rowCount(), 70)

    def test_different_recorded_customer_rates_and_object_fallback(self):
        self.shift(hours=2, rate=500, transport=1)
        self.shift(day="2026-10-22", hours=3, rate=550)
        self.shift(day="2026-10-23", hours=1, rate=0)
        report = self.window.generate_invoice()
        work = [line for line in report["cost_lines"] if line["label"] == "Arbete"]
        self.assertEqual([(line["quantity"], line["rate"]) for line in work], [(2, 500), (1, 520), (3, 550)])
        self.assertEqual(report["totals"]["work_amount"], 3170)
        self.assertEqual(report["totals"]["invoice_total"], 3620)
        self.assertEqual(report["recorded_trips"], 1)

    def test_missing_rates_and_missing_segments_keep_full_hours(self):
        self.shift(hours=4, kind="ob", transport=1)
        self.conn.execute("UPDATE objects SET rate=NULL,transport_rate=NULL WHERE id=39")
        self.conn.commit()
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 4)
        self.assertIsNone(report["totals"]["invoice_total"])
        self.assertIsNone(report["totals"]["work_amount"])
        self.assertIsNone(report["totals"]["transport_amount"])
        self.assertIsNone(report["prepared_rows"][0]["ob1_hours"])
        self.assertEqual(report["prepared_rows"][0]["hours"], 4)
        text = self.pdf(report).pages[0].extract_text()
        self.assertIn("Ej fastställt", text)
        self.assertIn("Preliminärt totalbelopp", text)

    def test_historical_object_name_and_all_journal_types_share_filters(self):
        self.conn.executescript("""
            CREATE TABLE object_name_history(sync_id TEXT,company TEXT,name TEXT);
            INSERT INTO object_name_history VALUES('object-t','Renatur','Tomteboda tidigare');
        """)
        self.shift(object_name="Tomteboda tidigare", hours=3)
        self.shift(kind="vacation", hours=2)
        self.shift(kind="sick", hours=1)
        self.shift(kind="atf", hours=1)
        self.shift(kind="holiday", hours=2)
        self.shift(day="2026-11-01", hours=10)
        self.shift(object_name="HOTEL Djurgården", hours=8)
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 3)
        self.assertEqual(len(report["journal_rows"]), 5)
        self.assertEqual(self.window.table_dagbok.rowCount(), 5)
        journal = "\n".join(page.extract_text() for page in self.pdf(report, journal_only=True).pages)
        for label in ("Semester", "Sjukfrånvaro", "Arbetstidsförkortning", "Röda dagar"):
            self.assertIn(label, journal)
        self.assertNotIn("Fakturaunderlag", journal)

    def test_fast_price_includes_ob_and_travel_once(self):
        self.ob()
        self.window.fast_pris_check.setChecked(True)
        self.window.fast_pris_input.setText("3000")
        report = self.window.generate_invoice()
        self.assertEqual(len(report["cost_lines"]), 1)
        self.assertEqual(report["totals"]["invoice_total"], 3000)
        self.assertEqual(report["totals"]["transport_amount"], 0)
        self.assertEqual(report["totals"]["hours"], 4)

    def test_pdf_long_text_escaped_and_no_external_translation(self):
        self.ob()
        self.conn.execute("UPDATE shifts SET comment=?", ("Montering <A> & B. " * 12,))
        self.conn.commit()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No external translation")):
            report = self.window.generate_invoice()
            journal = self.pdf(report).pages[-1].extract_text()
            self.assertIn("<A> & B", journal)
            self.assertEqual(self.window._journal_description_swedish("Демонтаж окон"), "demontering av fönster")
            with self.assertRaisesRegex(ValueError, "översättas"):
                self.window._journal_description_swedish("Неизвестноеслово")

    def test_unified_pages_share_filters_and_keep_journal_export(self):
        self.window.setup_reporting_pages()
        self.assertEqual(self.window.report_pages.count(), 2)
        self.assertIs(self.window.report_pages.widget(1), self.window.tab_dagbok)
        self.assertFalse(hasattr(self.window, "dag_comp_cb"))
        self.assertFalse(hasattr(self.window, "dag_date_from"))
        buttons = [button.text() for button in self.window.tab_invoice.findChildren(self.desktop.QPushButton)]
        self.assertIn("📄 Сформировать общий PDF (SV)", buttons)
        self.assertIn("📖 PDF только журнала (SV)", buttons)

    def test_other_ob_category_and_unknown_split_are_not_zero(self):
        self.ob()
        self.conn.execute("""
            UPDATE ob_segments SET category='saturday'
            WHERE premium_percent>0
        """)
        self.conn.commit()
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 4)
        self.assertEqual(report["prepared_rows"][0]["other_ob_categories"], {"saturday": 2})
        text = self.pdf(report).pages[0].extract_text()
        self.assertIn("Varav OB lördag", text)
        self.assertIn("OB lördag – tillägg", text)

    def test_fractional_hours_are_not_truncated_in_views_or_pdf(self):
        self.shift(hours=1.25)
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 1.25)
        self.assertEqual(report["totals"]["invoice_total"], 650)
        self.assertEqual(self.window.table_invoice.item(0, 1).text(), "1.25")
        self.assertEqual(self.window.table_dagbok.item(0, 3).text(), "1.25 ч")
        self.assertIn("1.25", self.window.dag_total_hours_lbl.text())
        pdf = self.pdf(report)
        for page in pdf.pages:
            self.assertIn("1,25", page.extract_text())

    def test_invalid_filters_clear_both_views_and_journal_totals(self):
        self.shift(hours=8)
        self.window.generate_invoice()
        self.window.inv_date_from.setDate(self.desktop.QDate(2026, 11, 1))
        with patch.object(self.desktop.QMessageBox, "warning") as warning:
            self.assertIsNone(self.window.generate_invoice())
        warning.assert_called_once()
        self.assertEqual(self.window.table_invoice.rowCount(), 0)
        self.assertEqual(self.window.table_dagbok.rowCount(), 0)
        self.assertIn("0.0", self.window.dag_total_hours_lbl.text())
        self.assertIsNone(self.window._invoice_report)

    def test_combined_and_preserved_journal_buttons_use_shared_export(self):
        self.ob()
        self.window.setup_reporting_pages()
        before = list(self.conn.iterdump())
        exported = []

        def write(report, filepath, font, bold_font, translate, *, journal_only=False):
            exported.append((journal_only, self.pdf(report, journal_only=journal_only)))

        with (
            patch.object(self.desktop, "write_customer_pdf", side_effect=write),
            patch.object(self.desktop.os, "makedirs"),
            patch.object(self.desktop.os, "startfile", create=True),
            patch.object(self.desktop.QMessageBox, "information"),
            patch.object(self.desktop.QMessageBox, "critical") as critical,
        ):
            for label in ("📄 Сформировать общий PDF (SV)", "📖 PDF только журнала (SV)"):
                button = next(
                    button for button in self.window.tab_invoice.findChildren(self.desktop.QPushButton)
                    if button.text() == label
                )
                button.click()
        critical.assert_not_called()
        self.assertEqual([(journal_only, len(pdf.pages)) for journal_only, pdf in exported], [(False, 2), (True, 1)])
        fragments = []
        exported[0][1].pages[0].extract_text(
            visitor_text=lambda text, cm, tm, font, size: fragments.append((text, size)),
        )
        self.assertTrue(any("Totalt antal timmar" in text and size == 12 for text, size in fragments))
        self.assertTrue(any("Preliminärt totalbelopp" in text and size == 12 for text, size in fragments))
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_hotel_entire_october_real_descriptions_export_without_changing_source(self):
        descriptions = [
            ("2026-10-20", 8, "Демонтаж,дверей и внутренних окон комнат"),
            ("2026-10-21", 8, "Демонтаж кухонных витяжок,демонтаж вентиляции и водяных труб"),
            ("2026-10-22", 6, "Демонтаж вентиляциии"),
        ]
        for day, hours, comment in descriptions:
            self.shift(day=day, hours=hours, transport=1, object_name="HOTEL Djurgården", comment=comment)
        self.window.inv_obj_cb.setCurrentIndex(self.window.inv_obj_cb.findData(84))
        before = list(self.conn.iterdump())
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 22)
        self.assertEqual(report["totals"]["work_amount"], 11440)
        self.assertEqual(report["totals"]["transport_amount"], 1350)
        self.assertEqual(report["totals"]["invoice_total"], 12790)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No network")), \
                patch.object(self.desktop.JournalTranslationDialog, "exec", side_effect=AssertionError("Known translations")):
            translated = self.window._collect_journal_translations(report)
        text = "\n".join(page.extract_text() for page in self.pdf({**report, "journal_translations": translated}).pages)
        self.assertIn("dörrar och invändiga fönster i rummen", text)
        for day, _, _ in descriptions:
            self.assertIn(day, text)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_manual_review_identifies_record_and_only_changes_pdf_translation(self):
        self.shift(comment="Неизвестноеслово <A>")
        self.shift(day="2026-10-22", comment="Неизвестноеслово <A>")
        before = list(self.conn.iterdump())
        report = self.window.generate_invoice()

        def review(dialog):
            table = dialog.findChild(self.desktop.QTableWidget)
            self.assertEqual(table.rowCount(), 2)
            self.assertIn("№1 · 2026-10-21", table.item(0, 0).text())
            self.assertIn("Andrii Piskun", table.item(0, 0).text())
            self.assertIn("Tomteboda", table.item(0, 0).text())
            self.assertEqual(table.item(0, 1).text(), "Неизвестноеслово <A>")
            self.assertFalse(table.item(0, 1).flags() & self.desktop.Qt.ItemFlag.ItemIsEditable)
            for number, translated in ((1, "Granskat arbete <A>"), (2, "Annat granskat arbete")):
                dialog.findChild(self.desktop.QTextEdit, "journal_translation_" + str(number)).setPlainText(translated)
            dialog.accept()
            return dialog.result()

        with patch.object(self.desktop.JournalTranslationDialog, "exec", review):
            translated = self.window._collect_journal_translations(report)
        self.assertEqual(translated, {"1": "Granskat arbete <A>", "2": "Annat granskat arbete"})
        journal = self.pdf({**report, "journal_translations": translated}).pages[-1].extract_text()
        self.assertIn("Granskat arbete <A>", journal)
        self.assertIn("Annat granskat arbete", journal)
        self.assertNotIn("journal_translations", report)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_manual_review_requires_nonempty_translation_without_cyrillic(self):
        self.shift(comment="Неизвестноеслово")
        report = self.window.generate_invoice()
        row = report["journal_rows"][0]
        dialog = self.desktop.JournalTranslationDialog([(row, row["comment"], "Unknown")], self.window)
        editor = dialog.findChild(self.desktop.QTextEdit, "journal_translation_1")
        for invalid in ("", " ", "Montering неизвестно"):
            editor.setPlainText(invalid)
            dialog.accept()
            self.assertNotEqual(dialog.result(), self.desktop.QDialog.DialogCode.Accepted)
            self.assertIn("№1 · 2026-10-21", dialog.error_label.text())
            self.assertEqual(dialog.translations, {})
        editor.setPlainText("Montering")
        dialog.accept()
        self.assertEqual(dialog.translations, {"1": "Montering"})

    def test_cancelled_translation_review_does_not_write_or_overwrite_pdf(self):
        self.shift(comment="Неизвестноеслово")
        before = list(self.conn.iterdump())
        with patch.object(self.desktop.JournalTranslationDialog, "exec", return_value=self.desktop.QDialog.DialogCode.Rejected), \
                patch.object(self.desktop, "write_customer_pdf") as writer, \
                patch.object(self.desktop.os, "makedirs") as mkdir, \
                patch.object(self.desktop.QMessageBox, "critical") as critical:
            self.window.export_invoice_to_pdf()
        writer.assert_not_called()
        mkdir.assert_not_called()
        critical.assert_not_called()
        self.assertIn("отменён", self.window.statusBar().currentMessage())
        self.assertEqual(list(self.conn.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
