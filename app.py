import sys
import os
import shutil
import traceback
import subprocess
import sqlite3
import requests
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QTabWidget,
    QFrame, QFormLayout, QDateEdit
)
from PyQt6.QtCore import Qt, QDate, QTimer
from PyQt6.QtGui import QIcon, QColor

# Импорты для генерации PDF
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Локальная база данных SQLite и папки
DB_FILE = "smart_report.db"
BACKUP_DIR = "backups"
REPORTS_DIR = "reports"
SALARIES_DIR = "salaries"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db_once():
    is_new_db = not os.path.exists(DB_FILE)
    
    for d in [REPORTS_DIR, SALARIES_DIR, BACKUP_DIR]:
        if not os.path.exists(d):
            os.makedirs(d)

    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            markning TEXT,
            company TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            salary_rate REAL DEFAULT 0.0,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    ''')

    # Авто-миграция для существующих баз (если колонка is_active еще не создана)
    cursor.execute("PRAGMA table_info(employees)")
    emp_columns = [col[1] for col in cursor.fetchall()]
    if 'is_active' not in emp_columns:
        cursor.execute("ALTER TABLE employees ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            company TEXT NOT NULL,
            object_name TEXT NOT NULL,
            hours REAL NOT NULL,
            rate REAL NOT NULL,
            transport REAL DEFAULT 0.0,
            comment TEXT,
            invoiced INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS balance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            description TEXT NOT NULL,
            change_amount REAL NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS company_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL
        )
    ''')

    cursor.execute("PRAGMA table_info(shifts)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'invoiced' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN invoiced INTEGER DEFAULT 0")

    cursor.execute("SELECT COUNT(*) FROM employees")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO employees (name, salary_rate) VALUES (?, ?)", ("Aliaksei Patonich", 0.0))

    if is_new_db:
        initial_data = [
            ("Privat", "Badbacken 2", "p1010"),
            ("Privat", "Koksgaatan 40", "p1011"),
            ("Privat", "Tallvagen 12", "p1001"),
            ("Dvaliks", "Klockargardsstigen 3", "***"),
            ("Privat", "Norra vagen 7", "p1013"),
            ("Privat", "Punchvagen 5", "p1012"),
            ("Privat", "Storholmsvagen 24", "p1014"),
            ("Privat", "Östra vägen 13", "p1017"),
            ("Privat", "Punchvagen 5 Ext", "p1016"),
            ("SBT", "Teknologgatan 7", "p2086"),
            ("SBT", "Bisittargatan 20A", "p2083"),
            ("Dvaliks", "Kronstigen 12", "***"),
            ("Dvaliks", "Gustav 3 boulevard 167", "Gustav 3"),
            ("SBT", "Krogtappan 97", "***"),
            ("Privat", "Badbacken 2 Altan", "p1018"),
            ("Privat", "Punchvagen 5 roof altan", "p1019"),
            ("SBT", "Hamarby Alle 3b", "***"),
            ("Privat", "Norra vagen 18 Niklas", "p1020"),
            ("Privat", "Tallvagen 12 Fasad Mal", "p1021"),
            ("Privat", "Badbacken 5 ext", "p1022"),
            ("Privat", "Östra vägen 7B", "p1023"),
            ("Privat", "Johan Hammarstrom", "p1024"),
            ("Privat", "Norra vagen 26 Katarina", "p1025"),
            ("Privat", "Tallvagen 14 Egil", "p1026"),
            ("SBT", "Kraftriket 21", "p2152"),
            ("SBT", "Teknikringen 35 Hogdel", "p2154"),
            ("Dvaliks", "Drottning vag 123", "***"),
            ("Pareto Properties AB", "Sonnebovagen 16 Sollentuna", "p1027"),
            ("Dvaliks", "Soderproken 16 Lidingo", "p1028"),
            ("SBT", "Alvsjoborgatan 1-3", "p2166"),
            ("Dvaliks", "Nyneshamn", "p1029"),
            ("SBT", "Vikingshillsvägen 15", "***"),
            ("Pareto Properties AB", "Alfred Nobels Alle 109", "p1030"),
            ("Renatur", "Tomteboda glass", "p1031"),
            ("Renatur", "Tomteboda doors", "p1032"),
            ("Privat", "Uddens vag 12 Tomas", "p1033"),
            ("SBT", "Karlsviksgatan 15", "p2128"),
            ("Renatur", "Tomteboda arbetsledning", "***"),
            ("Renatur", "Tomteboda ÄTA", "p1034"),
            ("Privat", "Tallvagen 11 1:116, Lidingö Kejill", "p1035"),
            ("Privat", "Kolarbacken 52 136 48 Handen/Vega Daniel", "p1036"),
            ("Renatur", "Tomteboda ÄTA Hotel", "****"),
            ("Privat", "Tallvagen 11 1:116, Lidingö Kejill ÄTA", "p1037")
        ]

        unique_companies = sorted(list(set(item[0] for item in initial_data)))
        for comp in unique_companies:
            cursor.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (comp,))

        for comp, name, mark in initial_data:
            cursor.execute('''
                INSERT OR IGNORE INTO objects (name, markning, company) VALUES (?, ?, ?)
            ''', (name, mark, comp))

    conn.commit()
    conn.close()

def parse_float(val_str, default=0.0):
    if not val_str:
        return default
    try:
        cleaned = str(val_str).strip().replace(',', '.')
        return float(cleaned)
    except ValueError:
        return default

# --- РЕГИСТРАЦИЯ ШРИФТА С ПОДДЕРЖКОЙ КИРИЛЛИЦЫ (ARIAL) ---
try:
    windows_font_path = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts', 'arial.ttf')
    if os.path.exists(windows_font_path):
        pdfmetrics.registerFont(TTFont('CustomArial', windows_font_path))
        pdfmetrics.registerFont(TTFont('CustomArialBold', os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts', 'arialbd.ttf')))
        DEFAULT_FONT = 'CustomArial'
        DEFAULT_FONT_BOLD = 'CustomArialBold'
    else:
        DEFAULT_FONT = 'Helvetica'
        DEFAULT_FONT_BOLD = 'Helvetica-Bold'
except Exception:
    DEFAULT_FONT = 'Helvetica'
    DEFAULT_FONT_BOLD = 'Helvetica-Bold'


# --- КОМФОРТНАЯ МЯГКАЯ ТЕМА ОФОРМЛЕНИЯ (SOFT GRAPHITE & BLUE ACCENT) ---
DARK_THEME_QSS = """
    QMainWindow, QWidget {
        background-color: #1e2229;
        color: #e2e8f0;
        font-family: 'Segoe UI', -apple-system, Arial, sans-serif;
        font-size: 13px;
    }
    QTabWidget::pane {
        border: 1px solid #2d3748;
        background-color: #242933;
        border-radius: 8px;
        top: -1px;
    }
    QTabBar::tab {
        background-color: #2a313d;
        color: #a0aec0;
        padding: 9px 16px;
        margin-right: 4px;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        border: 1px solid #323946;
        border-bottom: none;
        font-weight: 600;
    }
    QTabBar::tab:selected {
        background-color: #242933;
        color: #ffffff;
        border-bottom: 2px solid #4299e1;
    }
    QTabBar::tab:hover {
        background-color: #323a48;
        color: #ffffff;
    }
    QPushButton {
        background-color: #313c4e;
        color: #ffffff;
        border: 1px solid #4a5568;
        padding: 7px 14px;
        border-radius: 6px;
        font-weight: 600;
    }
    QPushButton:hover {
        background-color: #3c495f;
        border-color: #4299e1;
    }
    QPushButton:pressed {
        background-color: #4299e1;
        border-color: #3182ce;
    }
    QPushButton#danger {
        background-color: #632c2c;
        border-color: #9b3c3c;
    }
    QPushButton#danger:hover {
        background-color: #7a3434;
        border-color: #c53030;
    }
    QLineEdit, QComboBox, QDateEdit {
        background-color: #28303d;
        color: #f7fafc;
        border: 1px solid #3f4c60;
        padding: 6px 10px;
        border-radius: 6px;
        selection-background-color: #4299e1;
    }
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus {
        border: 1px solid #4299e1;
        background-color: #2d3748;
    }
    QComboBox::drop-down, QDateEdit::drop-down {
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 28px;
        border-left: 1px solid #3f4c60;
        border-top-right-radius: 6px;
        border-bottom-right-radius: 6px;
        background-color: #313c4e;
    }
    QComboBox::drop-down:hover, QDateEdit::drop-down:hover {
        background-color: #3c495f;
    }
    QComboBox::down-arrow, QDateEdit::down-arrow {
        image: none;
        width: 0px;
        height: 0px;
        border-left: 5px solid transparent;
        border-right: 5px solid transparent;
        border-top: 7px solid #4299e1;
        margin-right: 2px;
    }
    QComboBox QAbstractItemView {
        background-color: #28303d;
        color: #f7fafc;
        selection-background-color: #4299e1;
        border: 1px solid #3f4c60;
        outline: none;
    }
    QTableWidget, QTableView {
        background-color: #1a1f26;
        alternate-background-color: #212832;
        color: #e2e8f0;
        gridline-color: #2d3748;
        border: 1px solid #323946;
        border-radius: 6px;
        selection-background-color: #2b6cb0;
        selection-color: #ffffff;
    }
    QHeaderView::section {
        background-color: #28303d;
        color: #63b3ed;
        padding: 7px;
        border: 1px solid #323946;
        font-weight: bold;
        font-size: 12px;
    }
    QFrame#card {
        background-color: #242933;
        border: 1px solid #323946;
        border-radius: 8px;
        padding: 12px;
    }
    QScrollBar:vertical {
        background: #1e2229;
        width: 10px;
        margin: 0px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical {
        background: #4a5568;
        min-height: 20px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical:hover {
        background: #4299e1;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
    }
"""

def create_date_field(default_date_str=""):
    date_edit = QDateEdit()
    date_edit.setCalendarPopup(True)
    date_edit.setDisplayFormat("yyyy-MM-dd")
    if default_date_str:
        qdate = QDate.fromString(default_date_str, "yyyy-MM-dd")
        if qdate.isValid():
            date_edit.setDate(qdate)
    else:
        date_edit.setDate(QDate.currentDate())
        
    date_edit.text = lambda: date_edit.date().toString("yyyy-MM-dd")
    date_edit.setText = lambda val: date_edit.setDate(QDate.fromString(val, "yyyy-MM-dd"))
    return date_edit


class SmartReportApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SmartReport — Учет смен и отчеты")
        self.resize(1240, 880)
        self.setStyleSheet(DARK_THEME_QSS)

        init_db_once()
        self.init_ui()

        # Автоматическая фоновая синхронизация каждые 30 секунд (30000 мс)
        self.auto_sync_timer = QTimer(self)
        self.auto_sync_timer.timeout.connect(self.background_sync_with_cloud)
        self.auto_sync_timer.start(30000)

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self.on_tab_changed)
        main_layout.addWidget(self.tabs)

        self.tab_hours = QWidget()
        self.tab_salary = QWidget()
        self.tab_balance = QWidget()
        self.tab_expenses = QWidget()
        self.tab_invoice = QWidget()
        self.tab_dagbok = QWidget()
        self.tab_sprav = QWidget()
        
        self.tabs.addTab(self.tab_hours, "📝 Ввод часов")
        self.tabs.addTab(self.tab_salary, "💰 Зарплата")
        self.tabs.addTab(self.tab_balance, "⚖️ Баланс")
        self.tabs.addTab(self.tab_expenses, "🏢 Расходы и налоги")
        self.tabs.addTab(self.tab_invoice, "📄 Счёт для бухгалтера")
        self.tabs.addTab(self.tab_dagbok, "📖 Журнал работ")
        self.tabs.addTab(self.tab_sprav, "📚 Справочники")

        self.setup_hours_tab()
        self.setup_salary_tab()
        self.setup_balance_tab()
        self.setup_expenses_tab()
        self.setup_invoice_tab()
        self.setup_dagbok_tab()
        self.setup_sprav_tab()
        
        self.load_dropdowns()

    def on_tab_changed(self, index):
        self.load_dropdowns()
        if index == 1:
            self.load_salary_table_template()
        elif index == 2:
            self.load_balance_table()
        elif index == 3:
            self.load_expenses_table()
        elif index == 6:
            self.load_companies_table()
            self.load_objects_table()
            self.load_employees_table()

    def setup_hours_tab(self):
        layout = QVBoxLayout(self.tab_hours)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        card_layout.setSpacing(8)

        self.date_input = create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
        self.emp_cb = QComboBox()
        self.comp_cb = QComboBox()
        self.load_companies_into_combobox(self.comp_cb)
        
        self.obj_cb = QComboBox()
        self.load_objects_for_company(self.comp_cb.currentText(), self.obj_cb)
        self.comp_cb.currentTextChanged.connect(lambda comp: self.load_objects_for_company(comp, self.obj_cb))

        self.hours_input = QLineEdit("8.0")
        self.rate_input = QLineEdit()
        self.rate_input.setPlaceholderText("Например: 520")
        self.trans_input = QLineEdit("0")
        self.comment_input = QLineEdit()
        self.comment_input.setPlaceholderText("Описание выполненных работ...")

        card_layout.addRow("Дата (ГГГГ-ММ-ДД):", self.date_input)
        card_layout.addRow("Сотрудник:", self.emp_cb)
        card_layout.addRow("Фирма:", self.comp_cb)
        card_layout.addRow("Объект:", self.obj_cb)
        card_layout.addRow("Часы:", self.hours_input)
        card_layout.addRow("Фактурная ставка (крон/час):", self.rate_input)
        card_layout.addRow("Транспорт (kr):", self.trans_input)
        card_layout.addRow("Описание работ (Журнал):", self.comment_input)

        save_btn = QPushButton("💾 Сохранить смену")
        save_btn.clicked.connect(self.save_shift)
        card_layout.addRow(save_btn)

        layout.addWidget(card)
        
        shifts_ctrl_layout = QHBoxLayout()
        refresh_shifts_btn = QPushButton("🔄 Обновить данные")
        refresh_shifts_btn.clicked.connect(self.load_shifts_history)
        
        sync_cloud_btn = QPushButton("🌐 Синхронизация с облаком")
        sync_cloud_btn.setStyleSheet("background-color: #2b6cb0; border-color: #4299e1;")
        sync_cloud_btn.clicked.connect(self.sync_with_cloud)

        del_shift_btn = QPushButton("🗑 Удалить выбранную смену")
        del_shift_btn.setObjectName("danger")
        del_shift_btn.clicked.connect(self.delete_shift)

        shifts_ctrl_layout.addWidget(refresh_shifts_btn)
        shifts_ctrl_layout.addWidget(sync_cloud_btn)
        shifts_ctrl_layout.addStretch()
        shifts_ctrl_layout.addWidget(del_shift_btn)
        layout.addLayout(shifts_ctrl_layout)

        layout.addWidget(QLabel("<b>История смен в базе данных (зеленые — уже выставлены в счёт):</b>"))
        
        self.table_shifts = QTableWidget()
        self.table_shifts.setColumnCount(10)
        self.table_shifts.setHorizontalHeaderLabels(["ID", "Дата", "Сотрудник", "Фирма", "Объект", "Часы", "Факт. ставка", "Транспорт", "Комментарий", "Статус фактуры"])
        self.table_shifts.setColumnHidden(0, True)
        self.table_shifts.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_shifts.setAlternatingRowColors(True)
        layout.addWidget(self.table_shifts)
        self.load_shifts_history()

    def perform_sync_logic(self):
        """Общая логика двусторонней синхронизации"""
        CLOUD_URL = "https://smart-report-server.onrender.com"
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Сначала забираем новые смены, созданные через телефон / веб-форму
        try:
            response = requests.get(f"{CLOUD_URL}/get-unsynced", timeout=15)
            if response.status_code == 200:
                data = response.json()
                shifts_from_phone = data.get("shifts", [])
                
                if shifts_from_phone:
                    downloaded_ids = []
                    for s in shifts_from_phone:
                        cursor.execute('''
                            SELECT id FROM shifts 
                            WHERE date = ? AND employee = ? AND object_name = ? AND hours = ?
                        ''', (s['date'], s['employee'], s['object_name'], s['hours']))
                        
                        if not cursor.fetchone():
                            cursor.execute('''
                                INSERT INTO shifts (date, employee, company, object_name, hours, rate, transport, comment, invoiced)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                            ''', (
                                s['date'], s['employee'], s['company'], s['object_name'], 
                                s['hours'], s.get('rate', 0.0), s.get('transport', 0.0), s.get('comment', '')
                            ))
                            
                        downloaded_ids.append(s['id'])

                    conn.commit()

                    if downloaded_ids:
                        requests.post(f"{CLOUD_URL}/mark-synced", json={"ids": downloaded_ids}, timeout=10)
        except Exception:
            pass

        # 2. Собираем актуальные списки справочников из базы данных ПК
        cursor.execute("SELECT name FROM employees ORDER BY name")
        employees = [row[0] for row in cursor.fetchall()]
        
        cursor.execute("SELECT name FROM companies ORDER BY name")
        companies = [row[0] for row in cursor.fetchall()]
        
        # Передаем объекты как словари, чтобы сервер знал их маркировку и компанию!
        cursor.execute("SELECT name, markning, company FROM objects ORDER BY name")
        objects = [{"name": row[0], "markning": row[1], "company": row[2]} for row in cursor.fetchall()]
        
        # 3. Собираем все локальные смены для отправки в облако
        cursor.execute("SELECT date, employee, company, object_name, hours, rate, transport, comment FROM shifts")
        local_shift_rows = cursor.fetchall()
        
        conn.close()

        all_local_shifts = []
        for r in local_shift_rows:
            all_local_shifts.append({
                "date": r[0],
                "employee": r[1],
                "company": r[2],
                "object_name": r[3],
                "hours": r[4],
                "rate": r[5],
                "transport": r[6],
                "comment": r[7]
            })

        metadata_payload = {
            "employees": employees,
            "companies": companies,
            "objects": objects,
            "shifts": all_local_shifts
        }
        try:
            requests.post(f"{CLOUD_URL}/sync-desktop-data", json=metadata_payload, timeout=20)
        except Exception:
            pass

    def sync_with_cloud(self):
        """Ручная синхронизация по кнопке с уведомлением"""
        try:
            self.perform_sync_logic()
            self.load_shifts_history()
            QMessageBox.information(self, "Успех", "Синхронизация с облаком успешно завершена! Все сотрудники и данные обновлены.")
        except requests.exceptions.RequestException as e:
            QMessageBox.critical(self, "Ошибка сети", f"Не удалось подключиться к облаку:\n{e}")

    def background_sync_with_cloud(self):
        """Тихая фоновая синхронизация каждые 30 секунд"""
        try:
            self.perform_sync_logic()
            self.load_shifts_history()
        except Exception:
            pass

    def setup_salary_tab(self):
        layout = QVBoxLayout(self.tab_salary)
        
        total_card = QFrame()
        total_card.setObjectName("card")
        total_card.setStyleSheet("background-color: #242933; border: 2px solid #4299e1; border-radius: 8px; margin-bottom: 5px;")
        t_layout = QHBoxLayout(total_card)
        t_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.sal_summary_hours_lbl = QLabel("<b>Всего часов: 0.0</b>")
        self.sal_summary_hours_lbl.setStyleSheet("font-size: 14px; color: #63b3ed; border: none;")
        self.sal_summary_total_lbl = QLabel("<b>Итого к выплате: 0.00 kr</b>")
        self.sal_summary_total_lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #ffffff; border: none;")
        
        t_layout.addWidget(self.sal_summary_hours_lbl)
        t_layout.addSpacing(30)
        t_layout.addWidget(self.sal_summary_total_lbl)
        layout.addWidget(total_card)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QHBoxLayout(card)

        card_layout.addWidget(QLabel("<b>Период расчета:</b>"))
        self.sal_date_from = create_date_field("2026-10-01")
        self.sal_date_to = create_date_field("2026-10-31")
        calc_btn = QPushButton("🔄 Рассчитать зарплату")
        calc_btn.clicked.connect(self.calculate_salary)

        card_layout.addWidget(self.sal_date_from)
        card_layout.addWidget(QLabel("по"))
        card_layout.addWidget(self.sal_date_to)
        card_layout.addWidget(calc_btn)
        card_layout.addStretch()

        layout.addWidget(card)
        
        self.table_salary = QTableWidget()
        self.table_salary.setColumnCount(6)
        self.table_salary.setHorizontalHeaderLabels([
            "Сотрудник", "Часы", "Зарп. ставка", "Отпускные (13%)", "Налог (Skatt ~30%)", "Итого к выплате"
        ])
        self.table_salary.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_salary.setAlternatingRowColors(True)
        self.table_salary.cellChanged.connect(self.on_salary_cell_changed)
        layout.addWidget(self.table_salary)

    def load_salary_table_template(self):
        try:
            self.table_salary.cellChanged.disconnect(self.on_salary_cell_changed)
        except TypeError:
            pass

        d_from = self.sal_date_from.text().strip()
        d_to = self.sal_date_to.text().strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT name, COALESCE(salary_rate, 0.0) FROM employees ORDER BY name")
        emp_rows = cursor.fetchall()
        
        rates_map = {str(row[0]).strip().lower(): row[1] for row in emp_rows}
        orig_names = {str(row[0]).strip().lower(): row[0] for row in emp_rows}

        cursor.execute("""
            SELECT employee, SUM(hours) 
            FROM shifts 
            WHERE date >= ? AND date <= ? 
            GROUP BY employee
        """, (d_from, d_to))
        shift_rows = cursor.fetchall()
        conn.close()

        sorted_keys = sorted(list(rates_map.keys()))
        hours_map = {str(row[0]).strip().lower(): row[1] for row in shift_rows}

        self.table_salary.setRowCount(len(sorted_keys))
        
        sum_hrs_all = 0.0
        sum_net_all = 0.0

        for row_idx, key in enumerate(sorted_keys):
            emp_display_name = orig_names.get(key, key.title())
            hrs = hours_map.get(key, 0.0)
            salary_rate = rates_map.get(key, 0.0)
            
            sum_hrs_all += hrs

            work_sum = hrs * salary_rate
            vacation_pay = work_sum * 0.13
            tax = (work_sum + vacation_pay) * 0.3006
            net_total = (work_sum + vacation_pay) - tax
            
            sum_net_all += net_total

            item_emp = QTableWidgetItem(str(emp_display_name))
            item_emp.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item_emp.setFlags(item_emp.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, 0, item_emp)

            item_hrs = QTableWidgetItem(str(hrs))
            item_hrs.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item_hrs.setFlags(item_hrs.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, 1, item_hrs)

            item_rate = QTableWidgetItem(f"{salary_rate:.2f}")
            item_rate.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table_salary.setItem(row_idx, 2, item_rate)

            for col_i, val in enumerate([vacation_pay, tax, net_total], start=3):
                item_res = QTableWidgetItem(f"{val:.2f}")
                item_res.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item_res.setFlags(item_res.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table_salary.setItem(row_idx, col_i, item_res)

        self.sal_summary_hours_lbl.setText(f"<b>Всего часов: {sum_hrs_all:.1f}</b>")
        self.sal_summary_total_lbl.setText(f"<b>Итого к выплате: {sum_net_all:,.2f} kr</b>")

        self.table_salary.cellChanged.connect(self.on_salary_cell_changed)

    def on_salary_cell_changed(self, row, col):
        if col == 2:
            emp_item = self.table_salary.item(row, 0)
            rate_item = self.table_salary.item(row, 2)
            if not emp_item or not rate_item:
                return

            emp_name = emp_item.text().strip()
            new_rate = parse_float(rate_item.text(), 0.0)

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO employees (name, salary_rate) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET salary_rate = ?
            ''', (emp_name, new_rate, new_rate))
            conn.commit()
            conn.close()
            
            self.calculate_salary_row(row)
            self.background_sync_with_cloud()

    def calculate_salary_row(self, row_idx):
        emp_item = self.table_salary.item(row_idx, 0)
        hrs_item = self.table_salary.item(row_idx, 1)
        rate_item = self.table_salary.item(row_idx, 2)
        
        if not emp_item or not hrs_item or not rate_item:
            return
        
        hrs = parse_float(hrs_item.text(), 0.0)
        salary_rate = parse_float(rate_item.text(), 0.0)

        work_sum = hrs * salary_rate
        vacation_pay = work_sum * 0.13
        tax = (work_sum + vacation_pay) * 0.3006
        net_total = (work_sum + vacation_pay) - tax

        try:
            self.table_salary.cellChanged.disconnect(self.on_salary_cell_changed)
        except TypeError:
            pass

        for c_idx, val in enumerate([vacation_pay, tax, net_total], start=3):
            it = QTableWidgetItem(f"{val:.2f}")
            it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, c_idx, it)

        self.table_salary.cellChanged.connect(self.on_salary_cell_changed)
        
        sum_hrs = 0.0
        sum_net = 0.0
        for r in range(self.table_salary.rowCount()):
            h_it = self.table_salary.item(r, 1)
            n_it = self.table_salary.item(r, 5)
            if h_it: sum_hrs += parse_float(h_it.text(), 0.0)
            if n_it: sum_net += parse_float(n_it.text(), 0.0)
            
        self.sal_summary_hours_lbl.setText(f"<b>Всего часов: {sum_hrs:.1f}</b>")
        self.sal_summary_total_lbl.setText(f"<b>Итого к выплате: {sum_net:,.2f} kr</b>")

    def calculate_salary(self):
        self.load_salary_table_template()

    def setup_balance_tab(self):
        layout = QVBoxLayout(self.tab_balance)
        total_card = QFrame()
        total_card.setObjectName("card")
        total_card.setStyleSheet("background-color: #242933; border: 2px solid #4299e1; border-radius: 10px; margin-bottom: 5px;")
        
        total_layout = QVBoxLayout(total_card)
        total_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        total_title = QLabel("💰 ОБЩИЙ ИТОГ (БАЛАНС ПО ВСЕМ СОТРУДНИКАМ):")
        total_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        total_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #63b3ed; border: none;")
        
        self.total_sum_label = QLabel("0.00 kr")
        self.total_sum_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.total_sum_label.setStyleSheet("font-size: 26px; font-weight: bold; color: #ffffff; border: none;")
        
        total_layout.addWidget(total_title)
        total_layout.addWidget(self.total_sum_label)
        layout.addWidget(total_card)

        summary_card = QFrame()
        summary_card.setObjectName("card")
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.addWidget(QLabel("<b>📌 Текущий баланс (долг) по сотрудникам:</b>"))
        
        self.table_bal_summary = QTableWidget()
        self.table_bal_summary.setColumnCount(2)
        self.table_bal_summary.setHorizontalHeaderLabels(["Сотрудник", "Баланс / Долг (SEK)"])
        self.table_bal_summary.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_bal_summary.setFixedHeight(140)
        self.table_bal_summary.setAlternatingRowColors(True)
        summary_layout.addWidget(self.table_bal_summary)
        layout.addWidget(summary_card)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        card_layout.setSpacing(8)

        self.bal_date = create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
        self.bal_emp_cb = QComboBox()
        self.bal_desc = QLineEdit()
        self.bal_desc.setPlaceholderText("Например: Выдан аванс / Зарплата")
        self.bal_amount = QLineEdit()
        self.bal_amount.setPlaceholderText("Сумма (+ долг, - выплата)")

        card_layout.addRow("Дата:", self.bal_date)
        card_layout.addRow("Сотрудник:", self.bal_emp_cb)
        card_layout.addRow("Описание:", self.bal_desc)
        card_layout.addRow("Изменение (SEK):", self.bal_amount)

        add_bal_btn = QPushButton("➕ Добавить операцию")
        add_bal_btn.clicked.connect(self.add_balance_entry)
        card_layout.addRow(add_bal_btn)
        layout.addWidget(card)

        layout.addWidget(QLabel("<b>История операций баланса:</b>"))
        self.table_balance = QTableWidget()
        self.table_balance.setColumnCount(4)
        self.table_balance.setHorizontalHeaderLabels(["Дата", "Сотрудник", "Описание", "Изменение (SEK)"])
        self.table_balance.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_balance.setAlternatingRowColors(True)
        layout.addWidget(self.table_balance)

    def setup_expenses_tab(self):
        layout = QVBoxLayout(self.tab_expenses)
        
        exp_total_card = QFrame()
        exp_total_card.setObjectName("card")
        exp_total_card.setStyleSheet("background-color: #242933; border: 2px solid #4299e1; border-radius: 8px; margin-bottom: 5px;")
        et_layout = QHBoxLayout(exp_total_card)
        et_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.exp_summary_total_lbl = QLabel("<b>ОБЩАЯ СУММА РАСХОДОВ И НАЛОГОВ: 0.00 kr</b>")
        self.exp_summary_total_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #ffffff; border: none;")
        et_layout.addWidget(self.exp_summary_total_lbl)
        layout.addWidget(exp_total_card)

        filter_card = QFrame()
        filter_card.setObjectName("card")
        f_layout = QHBoxLayout(filter_card)
        
        f_layout.addWidget(QLabel("<b>Период с:</b>"))
        self.exp_date_from = create_date_field("2026-10-01")
        f_layout.addWidget(self.exp_date_from)
        
        f_layout.addWidget(QLabel("<b>по:</b>"))
        self.exp_date_to = create_date_field("2026-10-31")
        f_layout.addWidget(self.exp_date_to)
        
        auto_calc_btn = QPushButton("⚙️ Авторасчет FORA и налогов за период")
        auto_calc_btn.clicked.connect(self.auto_calculate_taxes_and_fora)
        f_layout.addWidget(auto_calc_btn)
        f_layout.addStretch()
        
        layout.addWidget(filter_card)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        card_layout.setSpacing(8)

        self.exp_date = create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
        self.exp_cat_cb = QComboBox()
        self.exp_cat_cb.addItems([
            "Инструменты и расходники",
            "Аренда склада / офиса",
            "Лизинг и топливо транспорта (Ford Transit)",
            "Бухгалтерия и страховка",
            "Прочие накладные расходы"
        ])
        
        self.exp_desc = QLineEdit()
        self.exp_desc.setPlaceholderText("Описание расхода (например, покупка дисков Makita)...")
        
        self.exp_amount = QLineEdit()
        self.exp_amount.setPlaceholderText("Сумма в SEK")

        card_layout.addRow("Дата расхода:", self.exp_date)
        card_layout.addRow("Категория расхода:", self.exp_cat_cb)
        card_layout.addRow("Описание:", self.exp_desc)
        card_layout.addRow("Сумма (SEK):", self.exp_amount)

        add_exp_btn = QPushButton("➕ Добавить расход в общую таблицу")
        add_exp_btn.clicked.connect(self.add_company_expense)
        card_layout.addRow(add_exp_btn)

        layout.addWidget(card)
        layout.addWidget(QLabel("<b>История расходов, налогов и отчислений компании:</b>"))
        
        self.table_expenses = QTableWidget()
        self.table_expenses.setColumnCount(4)
        self.table_expenses.setHorizontalHeaderLabels(["Дата", "Категория", "Описание", "Сумма (SEK)"])
        self.table_expenses.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_expenses.setAlternatingRowColors(True)
        layout.addWidget(self.table_expenses)

        exp_summary_box = QHBoxLayout()
        exp_summary_box.addStretch()
        
        del_exp_btn = QPushButton("🗑 Удалить выбранную запись")
        del_exp_btn.setObjectName("danger")
        del_exp_btn.clicked.connect(self.delete_company_expense)
        exp_summary_box.addWidget(del_exp_btn)
        layout.addLayout(exp_summary_box)

    def auto_calculate_taxes_and_fora(self):
        d_from = self.exp_date_from.text().strip()
        d_to = self.exp_date_to.text().strip()
        record_date = d_to  

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT name, COALESCE(salary_rate, 0.0) FROM employees")
        rates = {str(row[0]).strip().lower(): row[1] for row in cursor.fetchall()}

        cursor.execute("""
            SELECT employee, SUM(hours) as total_hours 
            FROM shifts 
            WHERE date >= ? AND date <= ? 
            GROUP BY employee
        """, (d_from, d_to))
        shift_rows = cursor.fetchall()

        if not shift_rows:
            conn.close()
            QMessageBox.warning(self, "Внимание", "За выбранный период нет отработанных смен для расчета отчислений!")
            return

        total_payroll = 0.0
        for row in shift_rows:
            emp_key = str(row[0]).strip().lower()
            hrs = row[1]
            rate = rates.get(emp_key, 0.0)
            total_payroll += (hrs * rate)

        if total_payroll <= 0:
            conn.close()
            QMessageBox.warning(self, "Внимание", "Фонд оплаты труда за период равен нулю. Проверьте ставки сотрудников!")
            return

        fora_amount = total_payroll * 0.052          # FORA (~5.2%)
        employer_tax = total_payroll * 0.3142        # Arbetsgivareavgift (~31.42%)

        cursor.execute("INSERT INTO company_expenses (date, category, description, amount) VALUES (?, ?, ?, ?)",
                       (record_date, "FORA и страхование рабочих (Kollektivavtal)", f"Авторасчет FORA за период {d_from} - {d_to}", fora_amount))
        
        cursor.execute("INSERT INTO company_expenses (date, category, description, amount) VALUES (?, ?, ?, ?)",
                       (record_date, "Налоги компании (Arbetsgivareavgift 31.42%)", f"Авторасчет социального налога за период {d_from} - {d_to}", employer_tax))

        conn.commit()
        conn.close()

        self.load_expenses_table()
        self.background_sync_with_cloud()
        QMessageBox.information(self, "Успех", f"Отчисления FORA ({fora_amount:,.2f} kr) и Arbetsgivareavgift ({employer_tax:,.2f} kr) успешно рассчитаны и добавлены в расходы компании!")

    def setup_invoice_tab(self):
        layout = QVBoxLayout(self.tab_invoice)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        card_layout.setSpacing(8)

        self.inv_date_from = create_date_field("2026-10-01")
        self.inv_date_to = create_date_field("2026-10-31")
        
        self.inv_comp_cb = QComboBox()
        self.load_companies_into_combobox(self.inv_comp_cb)
        
        self.inv_obj_cb = QComboBox()
        self.load_objects_for_company(self.inv_comp_cb.currentText(), self.inv_obj_cb)

        self.inv_comp_cb.currentTextChanged.connect(lambda comp: self.load_objects_for_company(comp, self.inv_obj_cb))

        card_layout.addRow("Период с:", self.inv_date_from)
        card_layout.addRow("Период по:", self.inv_date_to)
        card_layout.addRow("Фирма:", self.inv_comp_cb)
        card_layout.addRow("Объект:", self.inv_obj_cb)

        btn_layout = QHBoxLayout()
        gen_btn = QPushButton("📊 Сформировать отчёт для счёта")
        gen_btn.clicked.connect(self.generate_invoice)
        
        mark_invoiced_btn = QPushButton("✅ Закрыть период (Отметить как выставленный)")
        mark_invoiced_btn.setStyleSheet("background-color: #276749; border-color: #38a169;")
        mark_invoiced_btn.clicked.connect(self.mark_period_as_invoiced)

        unmark_invoiced_btn = QPushButton("❌ Отменить закрытие (Вернуть в работу)")
        unmark_invoiced_btn.setObjectName("danger")
        unmark_invoiced_btn.clicked.connect(self.unmark_period_as_invoiced)

        btn_layout.addWidget(gen_btn)
        btn_layout.addWidget(mark_invoiced_btn)
        btn_layout.addWidget(unmark_invoiced_btn)
        card_layout.addRow(btn_layout)

        layout.addWidget(card)
        layout.addWidget(QLabel("<b>Результат по объекту за период (зеленые строки уже закрыты и выставлены в счет):</b>"))
        
        self.table_invoice = QTableWidget()
        self.table_invoice.setColumnCount(7)
        self.table_invoice.setHorizontalHeaderLabels(["Сотрудник", "Часы", "Ставка", "Сумма (работа)", "Транспорт", "Итого", "Статус"])
        self.table_invoice.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_invoice.setAlternatingRowColors(True)
        layout.addWidget(self.table_invoice)

        summary_box = QHBoxLayout()
        self.inv_total_hours_lbl = QLabel("<b>Всего часов: 0</b>")
        self.inv_total_sum_lbl = QLabel("<b>Общая сумма: 0.00 kr</b>")
        summary_box.addWidget(self.inv_total_hours_lbl)
        summary_box.addStretch()
        summary_box.addWidget(self.inv_total_sum_lbl)
        layout.addLayout(summary_box)

    def mark_period_as_invoiced(self):
        d_from = self.inv_date_from.text().strip()
        d_to = self.inv_date_to.text().strip()
        comp = self.inv_comp_cb.currentText()
        obj_data = self.inv_obj_cb.currentData()
        obj = obj_data if obj_data else self.inv_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект для закрытия периода!")
            return

        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Отметить все смены по объекту <b>{obj}</b> за период <b>{d_from} — {d_to}</b> как выставленные в счет (закрыть период)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE shifts 
                SET invoiced = 1 
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
            ''', (comp, obj, d_from, d_to))
            conn.commit()
            conn.close()

            QMessageBox.information(self, "Успех", "Период успешно закрыт! Смены отмечены как выставленные.")
            self.generate_invoice()
            self.load_shifts_history()
            self.background_sync_with_cloud()

    def unmark_period_as_invoiced(self):
        d_from = self.inv_date_from.text().strip()
        d_to = self.inv_date_to.text().strip()
        comp = self.inv_comp_cb.currentText()
        obj_data = self.inv_obj_cb.currentData()
        obj = obj_data if obj_data else self.inv_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект для отмены закрытия!")
            return

        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Вернуть в работу (снять отметку счета) все смены по объекту <b>{obj}</b> за период <b>{d_from} — {d_to}</b>?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE shifts 
                SET invoiced = 0 
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
            ''', (comp, obj, d_from, d_to))
            conn.commit()
            conn.close()

            QMessageBox.information(self, "Успех", "Статус сброшен! Период снова переведен в статус «В работе».")
            self.generate_invoice()
            self.load_shifts_history()
            self.background_sync_with_cloud()

    def load_objects_for_company(self, comp_name, obj_cb):
        obj_cb.clear()
        if not comp_name:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, COALESCE(markning, '') FROM objects WHERE company = ? ORDER BY name", (comp_name,))
        rows = cursor.fetchall()
        conn.close()
        for row in rows:
            name = row[0]
            mark = row[1]
            display = f"{mark} | {name}" if mark else name
            obj_cb.addItem(display, userData=name)

    def setup_dagbok_tab(self):
        layout = QVBoxLayout(self.tab_dagbok)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        card_layout.setSpacing(8)

        self.dag_date_from = create_date_field("2026-10-01")
        self.dag_date_to = create_date_field("2026-10-31")
        
        self.dag_comp_cb = QComboBox()
        self.load_companies_into_combobox(self.dag_comp_cb)
        
        self.dag_obj_cb = QComboBox()
        self.load_objects_for_company(self.dag_comp_cb.currentText(), self.dag_obj_cb)

        self.dag_comp_cb.currentTextChanged.connect(lambda comp: self.load_objects_for_company(comp, self.dag_obj_cb))

        card_layout.addRow("Период с:", self.dag_date_from)
        card_layout.addRow("Период по:", self.dag_date_to)
        card_layout.addRow("Фирма:", self.dag_comp_cb)
        card_layout.addRow("Объект:", self.dag_obj_cb)

        btn_layout = QHBoxLayout()
        gen_dag_btn = QPushButton("📖 Сформировать журнал работ")
        gen_dag_btn.clicked.connect(self.generate_dagbok)
        
        export_pdf_btn = QPushButton("📄 Экспорт в PDF (SV)")
        export_pdf_btn.clicked.connect(self.export_dagbok_to_pdf)

        btn_layout.addWidget(gen_dag_btn)
        btn_layout.addWidget(export_pdf_btn)
        card_layout.addRow(btn_layout)

        layout.addWidget(card)
        layout.addWidget(QLabel("<b>Журнал выполненных работ (Dagbok):</b>"))
        
        self.table_dagbok = QTableWidget()
        self.table_dagbok.setColumnCount(4)
        self.table_dagbok.setHorizontalHeaderLabels(["Дата", "Сотрудник", "Отработано часов", "Описание выполненных работ"])
        
        header = self.table_dagbok.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        
        self.table_dagbok.setColumnWidth(0, 130)
        self.table_dagbok.setColumnWidth(1, 200)
        self.table_dagbok.setColumnWidth(2, 160)

        self.table_dagbok.setAlternatingRowColors(True)
        layout.addWidget(self.table_dagbok)

        dag_summary_box = QHBoxLayout()
        self.dag_total_hours_lbl = QLabel("<b>Итого часов по журналу: 0.0</b>")
        dag_summary_box.addWidget(self.dag_total_hours_lbl)
        dag_summary_box.addStretch()
        layout.addLayout(dag_summary_box)

    def generate_dagbok(self):
        d_from = self.dag_date_from.text().strip()
        d_to = self.dag_date_to.text().strip()
        comp = self.dag_comp_cb.currentText()
        obj_data = self.dag_obj_cb.currentData()
        obj = obj_data if obj_data else self.dag_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект для формирования журнала!")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT date, employee, hours, comment 
            FROM shifts
            WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
            ORDER BY date ASC, employee ASC
        ''', (comp, obj, d_from, d_to))
        rows = cursor.fetchall()
        conn.close()

        self.table_dagbok.setRowCount(len(rows))
        total_hours = 0.0

        for row_idx, row in enumerate(rows):
            date, emp, hrs, comment = row[0], row[1], row[2], row[3]
            total_hours += hrs
            
            it_date = QTableWidgetItem(str(date))
            it_date.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            
            it_emp = QTableWidgetItem(str(emp))
            it_emp.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            
            it_hrs = QTableWidgetItem(str(hrs))
            it_hrs.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            
            it_comm = QTableWidgetItem(str(comment or ""))
            it_comm.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            self.table_dagbok.setItem(row_idx, 0, it_date)
            self.table_dagbok.setItem(row_idx, 1, it_emp)
            self.table_dagbok.setItem(row_idx, 2, it_hrs)
            self.table_dagbok.setItem(row_idx, 3, it_comm)

        self.dag_total_hours_lbl.setText(f"<b>Итого часов по журналу: {total_hours:.1f}</b>")

    def export_dagbok_to_pdf(self):
        d_from = self.dag_date_from.text().strip()
        d_to = self.dag_date_to.text().strip()
        comp = self.dag_comp_cb.currentText()
        obj_data = self.dag_obj_cb.currentData()
        obj = obj_data if obj_data else self.dag_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект для формирования отчета!")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT date, employee, hours, comment 
            FROM shifts
            WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
            ORDER BY date ASC, employee ASC
        ''', (comp, obj, d_from, d_to))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            QMessageBox.warning(self, "Внимание", "За выбранный период по этому объекту нет записей!")
            return

        if not os.path.exists(REPORTS_DIR):
            os.makedirs(REPORTS_DIR)

        base_filename = f"Arbetsdagbok_{comp.replace(' ', '_')}_{obj.replace(' ', '_')}_{d_from}_till_{d_to}.pdf"
        filepath = os.path.join(REPORTS_DIR, base_filename)
        
        try:
            doc = SimpleDocTemplate(filepath, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
            story = []
            styles = getSampleStyleSheet()
            
            title_style = ParagraphStyle('TitleSV', parent=styles['Heading1'], fontName=DEFAULT_FONT_BOLD, fontSize=15, spaceAfter=4)
            subtitle_style = ParagraphStyle('SubTitleSV', parent=styles['Normal'], fontName=DEFAULT_FONT, fontSize=10, spaceAfter=14)

            story.append(Paragraph("<b>Bygger och renoverar i Sthlm AB</b>", title_style))
            story.append(Paragraph(f"<b>Arbetsdagbok (Dagbok)</b><br/>Beställare (Firma): <b>{comp}</b><br/>Objekt: <b>{obj}</b><br/>Period: {d_from} till {d_to}", subtitle_style))
            story.append(Spacer(1, 5))

            table_data = [["Datum", "Anställd", "Timmar", "Arbetsbeskrivning"]]
            total_hours = 0.0

            for row in rows:
                date, emp, hrs, comment = row[0], row[1], row[2], row[3]
                total_hours += hrs
                table_data.append([str(date), str(emp), f"{hrs:.1f}", str(comment or "")])

            table_data.append(["Totalt timmar:", "", f"{total_hours:.1f}", ""])

            t = Table(table_data, colWidths=[75, 110, 50, 300])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4299e1')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, -1), DEFAULT_FONT),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('TOPPADDING', (0, 0), (-1, 0), 6),
                ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.HexColor('#f9f9f9'), colors.white]),
                ('GRID', (0, 0), (-1, -2), 0.5, colors.HexColor('#dddddd')),
                ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#eeeeee')),
                ('FONTNAME', (0, -1), (-1, -1), DEFAULT_FONT_BOLD),
            ]))

            story.append(t)
            doc.build(story)
            QMessageBox.information(self, "Успех", f"PDF-отчет сохранен в папку reports:\n{base_filename}")
            
            if sys.platform == 'win32':
                os.startfile(filepath)
            elif sys.platform == 'darwin':
                subprocess.run(['open', filepath])
            else:
                subprocess.run(['xdg-open', filepath])

        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать PDF: {e}")

    def setup_sprav_tab(self):
        layout = QVBoxLayout(self.tab_sprav)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        top_layout = QHBoxLayout()
        top_layout.setSpacing(12)

        # Секция 1: Фирмы
        comp_card = QFrame()
        comp_card.setObjectName("card")
        comp_card_layout = QVBoxLayout(comp_card)
        comp_card_layout.setSpacing(6)
        comp_card_layout.addWidget(QLabel("<b>🏢 Фирмы (Заказчики):</b>"))
        
        comp_add_layout = QHBoxLayout()
        self.new_comp_input = QLineEdit()
        self.new_comp_input.setPlaceholderText("Название фирмы...")
        add_comp_btn = QPushButton("➕ Добавить")
        add_comp_btn.clicked.connect(self.add_company)
        comp_add_layout.addWidget(self.new_comp_input, stretch=3)
        comp_add_layout.addWidget(add_comp_btn, stretch=1)
        comp_card_layout.addLayout(comp_add_layout)

        self.table_companies = QTableWidget()
        self.table_companies.setColumnCount(1)
        self.table_companies.setHorizontalHeaderLabels(["Фирма"])
        self.table_companies.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        comp_card_layout.addWidget(self.table_companies, stretch=1)

        del_comp_btn = QPushButton("🗑 Удалить фирму")
        del_comp_btn.setObjectName("danger")
        del_comp_btn.clicked.connect(self.delete_company)
        comp_card_layout.addWidget(del_comp_btn)
        top_layout.addWidget(comp_card, stretch=1)

        # Секция 2: Сотрудники
        emp_card = QFrame()
        emp_card.setObjectName("card")
        emp_card_layout = QVBoxLayout(emp_card)
        emp_card_layout.setSpacing(6)
        emp_card_layout.addWidget(QLabel("<b>👥 Сотрудники:</b>"))
        
        emp_add_layout = QHBoxLayout()
        self.new_emp_input = QLineEdit()
        self.new_emp_input.setPlaceholderText("ФИО...")
        self.new_emp_rate_input = QLineEdit()
        self.new_emp_rate_input.setPlaceholderText("kr/ч...")
        self.new_emp_rate_input.setFixedWidth(80)
        add_emp_btn = QPushButton("➕ Добавить")
        add_emp_btn.clicked.connect(self.add_or_update_employee)
        emp_add_layout.addWidget(self.new_emp_input, stretch=2)
        emp_add_layout.addWidget(self.new_emp_rate_input, stretch=1)
        emp_add_layout.addWidget(add_emp_btn)
        emp_card_layout.addLayout(emp_add_layout)

        self.table_employees = QTableWidget()
        self.table_employees.setColumnCount(2)
        self.table_employees.setHorizontalHeaderLabels(["ФИО", "kr/ч"])
        self.table_employees.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        emp_card_layout.addWidget(self.table_employees, stretch=1)

        del_emp_btn = QPushButton("🗑 Удалить сотрудника")
        del_emp_btn.setObjectName("danger")
        del_emp_btn.clicked.connect(self.delete_employee)
        emp_card_layout.addWidget(del_emp_btn)
        top_layout.addWidget(emp_card, stretch=1)

        layout.addLayout(top_layout, stretch=1)

        # Нижняя панель (Управление объектами)
        obj_card = QFrame()
        obj_card.setObjectName("card")
        obj_card_layout = QVBoxLayout(obj_card)
        obj_card_layout.setSpacing(6)
        obj_card_layout.addWidget(QLabel("<b>🏗 Управление объектами:</b>"))

        obj_add_layout = QHBoxLayout()
        
        self.new_obj_comp_cb = QComboBox()
        self.load_companies_into_combobox(self.new_obj_comp_cb)

        self.new_obj_name_input = QLineEdit()
        self.new_obj_name_input.setPlaceholderText("Адрес / Название объекта...")
        self.new_obj_mark_input = QLineEdit()
        self.new_obj_mark_input.setPlaceholderText("Markning...")
        self.new_obj_mark_input.setFixedWidth(130)
        add_obj_btn = QPushButton("➕ Добавить объект")
        add_obj_btn.clicked.connect(self.add_object)

        obj_add_layout.addWidget(self.new_obj_comp_cb, stretch=2)
        obj_add_layout.addWidget(self.new_obj_name_input, stretch=3)
        obj_add_layout.addWidget(self.new_obj_mark_input, stretch=1)
        obj_add_layout.addWidget(add_obj_btn)
        obj_card_layout.addLayout(obj_add_layout)
        
        self.table_objects = QTableWidget()
        self.table_objects.setColumnCount(3)
        self.table_objects.setHorizontalHeaderLabels(["Фирма", "Объект / Адрес", "Markning"])
        self.table_objects.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        obj_card_layout.addWidget(self.table_objects, stretch=1)

        del_obj_btn = QPushButton("🗑 Удалить выбранный объект")
        del_obj_btn.setObjectName("danger")
        del_obj_btn.clicked.connect(self.delete_object)
        obj_card_layout.addWidget(del_obj_btn)

        layout.addWidget(obj_card, stretch=1)

    def load_companies_into_combobox(self, cb):
        current_val = cb.currentText()
        cb.clear()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM companies ORDER BY name")
        rows = cursor.fetchall()
        conn.close()
        for row in rows:
            cb.addItem(row[0])
        if current_val:
            idx = cb.findText(current_val)
            if idx >= 0:
                cb.setCurrentIndex(idx)

    def add_company(self):
        comp_name = self.new_comp_input.text().strip()
        if not comp_name:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO companies (name) VALUES (?)", (comp_name,))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        conn.close()
        self.new_comp_input.clear()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def load_companies_table(self):
        if hasattr(self, 'table_companies'):
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM companies ORDER BY name")
            rows = cursor.fetchall()
            conn.close()
            self.table_companies.setRowCount(len(rows))
            for row_idx, row in enumerate(rows):
                self.table_companies.setItem(row_idx, 0, QTableWidgetItem(row[0]))

    def delete_company(self):
        selected = self.table_companies.currentRow()
        if selected < 0:
            return
        comp_name = self.table_companies.item(selected, 0).text()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM companies WHERE name = ?', (comp_name,))
        conn.commit()
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def add_object(self):
        comp = self.new_obj_comp_cb.currentText().strip()
        name = self.new_obj_name_input.text().strip()
        mark = self.new_obj_mark_input.text().strip()
        if not name or not comp:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму из списка и укажите название объекта!")
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO objects (name, markning, company) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET markning = ?, company = ?
            ''', (name, mark, comp, mark, comp))
            conn.commit()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось добавить объект: {e}")
        conn.close()
        self.new_obj_name_input.clear()
        self.new_obj_mark_input.clear()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def delete_object(self):
        selected = self.table_objects.currentRow()
        if selected < 0:
            return
        obj_name = self.table_objects.item(selected, 1).text()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM objects WHERE name = ?', (obj_name,))
        conn.commit()
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def load_dropdowns(self):
        conn = get_db_connection()
        cursor = conn.cursor()

        if hasattr(self, 'emp_cb'):
            self.emp_cb.clear()
            cursor.execute("SELECT name FROM employees ORDER BY name")
            rows = cursor.fetchall()
            for row in rows:
                self.emp_cb.addItem(row[0])

        if hasattr(self, 'bal_emp_cb'):
            self.bal_emp_cb.clear()
            cursor.execute("SELECT name FROM employees ORDER BY name")
            rows = cursor.fetchall()
            for row in rows:
                self.bal_emp_cb.addItem(row[0])

        for cb in [getattr(self, 'comp_cb', None), getattr(self, 'inv_comp_cb', None), getattr(self, 'dag_comp_cb', None), getattr(self, 'new_obj_comp_cb', None)]:
            if cb:
                self.load_companies_into_combobox(cb)

        if hasattr(self, 'obj_cb') and hasattr(self, 'comp_cb'):
            self.load_objects_for_company(self.comp_cb.currentText(), self.obj_cb)

        if hasattr(self, 'inv_obj_cb') and hasattr(self, 'inv_comp_cb'):
            self.load_objects_for_company(self.inv_comp_cb.currentText(), self.inv_obj_cb)

        if hasattr(self, 'dag_obj_cb') and hasattr(self, 'dag_comp_cb'):
            self.load_objects_for_company(self.dag_comp_cb.currentText(), self.dag_obj_cb)

        conn.close()

        if hasattr(self, 'table_employees'):
            self.load_employees_table()
        if hasattr(self, 'table_objects'):
            self.load_objects_table()
        if hasattr(self, 'table_companies'):
            self.load_companies_table()

    def load_objects_table(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT company, name, markning FROM objects ORDER BY company, name")
        rows = cursor.fetchall()
        conn.close()
        self.table_objects.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            self.table_objects.setItem(row_idx, 0, QTableWidgetItem(row[0] or ""))
            self.table_objects.setItem(row_idx, 1, QTableWidgetItem(row[1]))
            self.table_objects.setItem(row_idx, 2, QTableWidgetItem(row[2] or ""))

    def load_employees_table(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, salary_rate FROM employees ORDER BY name")
        rows = cursor.fetchall()
        conn.close()
        self.table_employees.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            rate = row[1]
            self.table_employees.setItem(row_idx, 0, QTableWidgetItem(row[0]))
            self.table_employees.setItem(row_idx, 1, QTableWidgetItem(f"{rate:.2f}" if rate else "0.00"))

    def add_or_update_employee(self):
        name = self.new_emp_input.text().strip()
        rate_val = parse_float(self.new_emp_rate_input.text(), 0.0)
        if not name:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO employees (name, salary_rate) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET salary_rate = ?
        ''', (name, rate_val, rate_val))
        conn.commit()
        conn.close()
        self.new_emp_input.clear()
        self.new_emp_rate_input.clear()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def delete_employee(self):
        selected = self.table_employees.currentRow()
        if selected < 0:
            return
        emp_name = self.table_employees.item(selected, 0).text()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM employees WHERE name = ?', (emp_name,))
        conn.commit()
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def save_shift(self):
        date = self.date_input.text().strip()
        emp = self.emp_cb.currentText()
        comp = self.comp_cb.currentText()
        obj_name = self.obj_cb.currentData() or self.obj_cb.currentText()
        
        hours_val = parse_float(self.hours_input.text(), -1)
        rate_val = parse_float(self.rate_input.text(), -1)
        trans_val = parse_float(self.trans_input.text(), 0.0)
        comment = self.comment_input.text().strip()

        if hours_val < 0 or rate_val < 0:
            QMessageBox.warning(self, "Ошибка", "Проверьте правильность числовых полей (часы и ставка)!")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO shifts (date, employee, company, object_name, hours, rate, transport, comment, invoiced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (date, emp, comp, obj_name, hours_val, rate_val, trans_val, comment))
        conn.commit()
        conn.close()
        
        # Мгновенная отправка в облако при сохранении смены
        self.background_sync_with_cloud()
        
        QMessageBox.information(self, "Успех", "Смена успешно сохранена и отправлена в облако!")
        self.load_shifts_history()

    def load_shifts_history(self):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT id, date, employee, company, object_name, hours, rate, transport, comment, invoiced FROM shifts ORDER BY id DESC LIMIT 50")
            rows = cursor.fetchall()
            conn.close()

            self.table_shifts.setRowCount(len(rows))
            for row_idx, row in enumerate(rows):
                is_inv = row[9] == 1
                status_text = "✅ Выставлен" * is_inv or "⏳ В работе"

                for col_idx, val in enumerate([row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8] or "", status_text]):
                    it = QTableWidgetItem(str(val))
                    if is_inv:
                        it.setBackground(QColor("#1c4532"))
                    self.table_shifts.setItem(row_idx, col_idx, it)
            
            self.load_dropdowns()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось обновить данные: {e}")

    def delete_shift(self):
        selected = self.table_shifts.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "Внимание", "Выберите строку смены в таблице для удаления!")
            return
        
        shift_id_item = self.table_shifts.item(selected, 0)
        if not shift_id_item:
            return
        
        shift_id = shift_id_item.text()
        
        reply = QMessageBox.question(
            self, "Подтверждение", 
            "Вы действительно хотите удалить выбранную смену из базы данных?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM shifts WHERE id = ?', (shift_id,))
            conn.commit()
            conn.close()
            self.load_shifts_history()
            self.background_sync_with_cloud()

    def generate_invoice(self):
        d_from = self.inv_date_from.text().strip()
        d_to = self.inv_date_to.text().strip()
        comp = self.inv_comp_cb.currentText()
        obj_data = self.inv_obj_cb.currentData()
        obj = obj_data if obj_data else self.inv_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект для формирования отчёта!")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT employee, SUM(hours) as total_hours, AVG(rate) as avg_rate, SUM(hours * rate) as work_sum, SUM(transport) as total_trans, MAX(invoiced) as invoiced_status
            FROM shifts
            WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
            GROUP BY employee
            ORDER BY employee
        ''', (comp, obj, d_from, d_to))
        rows = cursor.fetchall()
        conn.close()

        self.table_invoice.setRowCount(len(rows))
        total_hours_all = 0.0
        total_sum_all = 0.0

        for row_idx, row in enumerate(rows):
            emp = row[0]
            hrs = row[1]
            rate = row[2]
            work_sum = row[3]
            transport = row[4]
            row_total = work_sum + transport
            is_inv = row[5] == 1
            status_str = "✅ Выставлен" * is_inv or "⏳ В работе"
            
            total_hours_all += hrs
            total_sum_all += row_total

            for col_idx, val in enumerate([emp, hrs, f"{rate:.2f}", f"{work_sum:.2f}", f"{transport:.2f}", f"{row_total:.2f}", status_str]):
                it = QTableWidgetItem(str(val))
                if is_inv:
                    it.setBackground(QColor("#1c4532"))
                self.table_invoice.setItem(row_idx, col_idx, it)

        self.inv_total_hours_lbl.setText(f"<b>Всего часов: {total_hours_all:.1f}</b>")
        self.inv_total_sum_lbl.setText(f"<b>Общая сумма: {total_sum_all:,.2f} kr</b>")

    def add_balance_entry(self):
        date = self.bal_date.text().strip()
        emp = self.bal_emp_cb.currentText()
        desc = self.bal_desc.text().strip()
        amt = parse_float(self.bal_amount.text(), None)
        if amt is None:
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO balance (date, employee, description, change_amount) VALUES (?, ?, ?, ?)',
                       (date, emp, desc, amt))
        conn.commit()
        conn.close()
        self.bal_desc.clear()
        self.bal_amount.clear()
        self.load_balance_table()
        self.background_sync_with_cloud()

    def load_balance_table(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT employee, SUM(change_amount) as total_bal
            FROM balance
            GROUP BY employee
            HAVING SUM(change_amount) != 0
            ORDER BY SUM(change_amount) DESC
        ''')
        summary_rows = cursor.fetchall()
        self.table_bal_summary.setRowCount(len(summary_rows))
        grand_total = 0.0
        for r_idx, row in enumerate(summary_rows):
            emp, total = row[0], row[1]
            grand_total += total
            self.table_bal_summary.setItem(r_idx, 0, QTableWidgetItem(str(emp)))
            self.table_bal_summary.setItem(r_idx, 1, QTableWidgetItem(f"{total:,.2f} kr"))

        self.total_sum_label.setText(f"{grand_total:,.2f} kr")
        cursor.execute("SELECT date, employee, description, change_amount FROM balance ORDER BY id DESC")
        rows = cursor.fetchall()
        conn.close()
        self.table_balance.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            for col_idx, val in enumerate(row):
                self.table_balance.setItem(row_idx, col_idx, QTableWidgetItem(str(val)))

    def add_company_expense(self):
        date = self.exp_date.text().strip()
        category = self.exp_cat_cb.currentText()
        desc = self.exp_desc.text().strip()
        amt = parse_float(self.exp_amount.text(), None)
        if amt is None:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO company_expenses (date, category, description, amount) VALUES (?, ?, ?, ?)',
                       (date, category, desc, amt))
        conn.commit()
        conn.close()
        self.exp_desc.clear()
        self.exp_amount.clear()
        self.load_expenses_table()
        self.background_sync_with_cloud()

    def load_expenses_table(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT date, category, description, amount FROM company_expenses ORDER BY id DESC")
        rows = cursor.fetchall()
        conn.close()
        self.table_expenses.setRowCount(len(rows))
        total_exp = 0.0
        for row_idx, row in enumerate(rows):
            date, cat, desc, amt = row[0], row[1], row[2], row[3]
            total_exp += amt
            self.table_expenses.setItem(row_idx, 0, QTableWidgetItem(str(date)))
            self.table_expenses.setItem(row_idx, 1, QTableWidgetItem(str(cat)))
            self.table_expenses.setItem(row_idx, 2, QTableWidgetItem(str(desc)))
            self.table_expenses.setItem(row_idx, 3, QTableWidgetItem(f"{amt:,.2f} kr"))
        
        self.exp_summary_total_lbl.setText(f"<b>ОБЩАЯ СУММА РАСХОДОВ И НАЛОГОВ: {total_exp:,.2f} kr</b>")

    def delete_company_expense(self, index=None):
        selected = self.table_expenses.currentRow()
        if selected < 0:
            return
        date_val = self.table_expenses.item(selected, 0).text()
        cat_val = self.table_expenses.item(selected, 1).text()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM company_expenses WHERE date = ? AND category = ?', (date_val, cat_val))
        conn.commit()
        conn.close()
        self.load_expenses_table()
        self.background_sync_with_cloud()

if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        if os.path.exists("logo.ico"):
            app.setWindowIcon(QIcon("logo.ico"))
        window = SmartReportApp()
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        print("\n--- ОШИБКА ---")
        traceback.print_exc()
        input("\nНажмите Enter...")