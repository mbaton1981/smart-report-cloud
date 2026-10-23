"""Desktop appearance only; no database, authorization or document changes."""
import json
import logging
import os
from pathlib import Path
import re

from PyQt6.QtCore import QIODevice, QObject, QSaveFile, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFontDatabase, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton

COLORS = {
    "#1e2229": "#edf2f6", "#242933": "#f7f9fb", "#2a313d": "#e4ebf1",
    "#323a48": "#dce7ef", "#323946": "#ccd8e1", "#2d3748": "#dce5ec",
    "#e2e8f0": "#2c4052", "#a0aec0": "#52697b", "#ffffff": "#2c4052",
    "#f7fafc": "#2c4052", "#313c4e": "#e2ebf2", "#4a5568": "#b7cad9",
    "#3c495f": "#d4e2ed", "#2b6cb0": "#52718a", "#3182ce": "#5c7e99",
    "#4299e1": "#7d9bb1", "#63b3ed": "#395d77", "#90cdf4": "#526f87",
    "#252b35": "#e8edf1", "#718096": "#657888", "#632c2c": "#f1e5e5",
    "#9b3c3c": "#d8bfc0", "#7a3434": "#e7d2d3", "#c53030": "#9c4a50",
    "#28303d": "#fcfdfe", "#3f4c60": "#b9cad7", "#64748b": "#8ca5b8",
    "#1a1f26": "#f9fbfd", "#252d38": "#edf3f7", "#303c4b": "#e3edf4",
    "#2d567c": "#cadde9", "#1c4532": "#e4efe9", "#1a202c": "#fcfdfe",
    "#276749": "#4f705c", "#38a169": "#61836b", "#c6f6d5": "#8fb19a",
    "#68d391": "#376b4b", "#f6ad55": "#885c21", "#fca5a5": "#9c3e46",
    "#cbd5e1": "#52697b", "#cbd5e0": "#b9cad7",
}
BACKGROUND_ROLE = int(Qt.ItemDataRole.UserRole) + 271
FOREGROUND_ROLE = BACKGROUND_ROLE + 1


def current_theme():
    application = QApplication.instance()
    manager = getattr(application, "_smartreport_theme", None)
    return manager.name if manager is not None else "dark"


def color(value):
    return QColor(COLORS.get(value.lower(), value) if current_theme() == "light" else value)


def style_text(style, theme=None):
    if (theme or current_theme()) != "light":
        return style
    return re.sub(r"#[0-9a-fA-F]{6}\b", lambda match: COLORS.get(match[0].lower(), match[0]), style)


def apply_style(widget, dark_style):
    widget.setProperty("smartreportDarkStyle", dark_style)
    _inline_style(widget, style_text(dark_style))


def _inline_style(widget, style):
    widget.setStyleSheet(style)
    # Qt replaces combo delegates while polishing a new stylesheet.
    delegate = getattr(widget, "_smartreport_delegate", None)
    if delegate is not None:
        widget.setItemDelegate(delegate)


def item_color(item, value, *, foreground=False):
    role = FOREGROUND_ROLE if foreground else BACKGROUND_ROLE
    item.setData(role, value)
    if foreground:
        item.setForeground(color(value))
    else:
        item.setBackground(color(value))


def _logo():
    pixmap = QPixmap(36, 36)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#52718a"))
    painter.drawRoundedRect(0, 0, 36, 36, 8, 8)
    painter.setBrush(QColor("#eaf2f8"))
    for x, y, height in ((9, 21, 7), (16, 15, 13), (23, 8, 20)):
        painter.drawRoundedRect(x, y, 4, height, 1, 1)
    painter.end()
    return pixmap


EXTRA = """
QMenu, QComboBox QAbstractItemView, QCalendarWidget QAbstractItemView {
    background-color: #28303d; color: #e2e8f0;
    selection-background-color: #2b6cb0; selection-color: #ffffff;
}
QMenu::item:selected { background-color: #2b6cb0; color: #ffffff; }
QGroupBox { border: 1px solid #323946; border-radius: 7px; margin-top: 12px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QPushButton:disabled, QPushButton#primaryAction:disabled, QPushButton#secondaryAction:disabled,
QPushButton#danger:disabled, QPushButton#deleteShiftButton:disabled, QPushButton#editShiftButton:disabled {
    background-color: #252b35; color: #718096; border-color: #323946;
}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QTextEdit:disabled { background-color: #252b35; color: #718096; }
QLineEdit[readOnly="true"] { background-color: #242933; color: #a0aec0; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px; height: 16px; border: 1px solid #718096;
    background-color: #28303d; border-radius: 3px;
}
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: #2b6cb0; border: 2px solid #90cdf4;
}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled { background-color: #252b35; border-color: #4a5568; }
QScrollBar:vertical { background-color: #242933; width: 12px; margin: 0; }
QScrollBar:horizontal { background-color: #242933; height: 12px; margin: 0; }
QScrollBar::handle { background-color: #4a5568; border-radius: 5px; min-width: 24px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QStatusBar { background-color: #242933; color: #a0aec0; border-top: 1px solid #323946; }
QFrame#smartreportHeader { background-color: #242933; border: 1px solid #323946; border-radius: 8px; }
QComboBox::down-arrow, QDateEdit::down-arrow {
    image: url("__DARK_CHEVRON__"); width: 12px; height: 8px; border: none;
}
QPushButton#successAction { background-color: #276749; color: #ffffff; border-color: #38a169; }
QPushButton#successAction:disabled { background-color: #252b35; color: #718096; border-color: #323946; }
QLabel#smartreportBrand { font-size: 16px; font-weight: 600; }
QLabel#smartreportCaption { color: #a0aec0; font-size: 12px; }
"""
LIGHT_EXTRA = """
QPushButton#primaryAction { background-color: #52718a; color: #ffffff; border-color: #52718a; }
QPushButton#primaryAction:hover { background-color: #45647d; color: #ffffff; }
QPushButton#primaryAction:disabled { background-color: #e8edf1; color: #657888; border-color: #ccd8e1; }
QPushButton#successAction { background-color: #4f705c; color: #ffffff; border-color: #61836b; }
QPushButton#successAction:disabled { background-color: #e8edf1; color: #657888; border-color: #ccd8e1; }
QTableWidget::item:selected, QTableView::item:selected { background-color: #d6e5f0; color: #243b4e; }
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextEdit,
QComboBox QAbstractItemView, QCalendarWidget QAbstractItemView {
    selection-background-color: #d6e5f0; selection-color: #243b4e;
    placeholder-text-color: #657888;
}
QMenu::item:selected { background-color: #d6e5f0; color: #243b4e; }
QComboBox::down-arrow, QDateEdit::down-arrow {
    image: url("__CHEVRON__"); width: 12px; height: 8px; border: none;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("__UP__"); }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("__CHEVRON__"); }
"""


class ThemeManager(QObject):
    changed = pyqtSignal(str)

    def __init__(self, application, dark_style, settings_path=None):
        super().__init__(application)
        self.application = application
        self.dark_style = dark_style
        if os.name == "nt":
            for filename in ("segoeui.ttf", "segoeuib.ttf"):
                font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / filename
                if font.is_file() and QFontDatabase.addApplicationFont(str(font)) < 0:
                    logging.getLogger(__name__).warning("Unable to register desktop UI font %s", filename)
        if settings_path is None:
            import desktop_auth
            settings_path = desktop_auth.app_data_dir() / "appearance.json"
        self.settings_path = Path(settings_path)
        self.name = "light"
        if self.settings_path.exists():
            try:
                stored = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if stored.get("theme") not in {"light", "dark"}:
                    raise ValueError("Неизвестная тема оформления.")
                self.name = stored["theme"]
            except (OSError, ValueError, AttributeError) as error:
                QMessageBox.warning(None, "Настройка темы", f"Не удалось прочитать тему. Используется светлая тема.\n{error}")
        self.apply()

    def stylesheet(self):
        import sys
        assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
        style = style_text(self.dark_style + EXTRA, self.name)
        style = style.replace("__DARK_CHEVRON__", (assets / "spin-down.svg").as_posix())
        if self.name == "light":
            style += LIGHT_EXTRA.replace("__CHEVRON__", (assets / "theme-chevron.svg").as_posix()).replace(
                "__UP__", (assets / "theme-up.svg").as_posix())
        return style

    def apply(self):
        self.application.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
        self.application.setStyleSheet(self.stylesheet())
        from PyQt6.QtWidgets import QTableWidget
        for widget in self.application.allWidgets():
            inline = widget.property("smartreportDarkStyle")
            if inline is not None:
                _inline_style(widget, style_text(inline, self.name))
            if isinstance(widget, QTableWidget):
                # Appearance updates must not invoke editable-table persistence handlers.
                with QSignalBlocker(widget):
                    for row in range(widget.rowCount()):
                        for column in range(widget.columnCount()):
                            item = widget.item(row, column)
                            if item is None:
                                continue
                            for role, setter in ((BACKGROUND_ROLE, item.setBackground), (FOREGROUND_ROLE, item.setForeground)):
                                original = item.data(role)
                                if original:
                                    value = COLORS.get(original.lower(), original) if self.name == "light" else original
                                    setter(QBrush(QColor(value)))
                widget.viewport().update()
        self.changed.emit(self.name)

    def toggle(self):
        desired = "dark" if self.name == "light" else "light"
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"theme": desired}, ensure_ascii=False).encode("utf-8")
            output = QSaveFile(str(self.settings_path))
            if not output.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(output.errorString())
            if output.write(data) != len(data):
                output.cancelWriting()
                raise OSError("Не удалось записать настройку полностью.")
            if not output.commit():
                raise OSError(output.errorString())
        except OSError as error:
            QMessageBox.warning(None, "Тема не изменена", f"Не удалось сохранить выбор темы:\n{error}")
            return False
        self.name = desired
        self.apply()
        return True


def ensure_theme(dark_style):
    application = QApplication.instance()
    if not hasattr(application, "_smartreport_theme"):
        application._smartreport_theme = ThemeManager(application, dark_style)
    return application._smartreport_theme


class ThemeToggle(QPushButton):
    def __init__(self, manager):
        super().__init__()
        self.setObjectName("themeToggle")
        self.setAccessibleName("Переключить светлую и тёмную тему")
        self.setToolTip("Смена без перезапуска. Выбор сохраняется для этого профиля.")
        self.clicked.connect(manager.toggle)
        manager.changed.connect(self.update_name)
        self.update_name(manager.name)

    def update_name(self, name):
        self.setText("Светлая тема" if name == "dark" else "Тёмная тема")


def header(parent=None):
    application = QApplication.instance()
    manager = ensure_theme(application.styleSheet())
    panel = QFrame(parent)
    panel.setObjectName("smartreportHeader")
    row = QHBoxLayout(panel)
    row.setContentsMargins(12, 8, 12, 8)
    icon = QLabel()
    icon.setPixmap(_logo())
    icon.setAccessibleName("Логотип SmartReport")
    row.addWidget(icon)
    brand = QLabel("SmartReport")
    brand.setObjectName("smartreportBrand")
    row.addWidget(brand)
    toggle = ThemeToggle(manager)
    row.addSpacing(16)
    row.addWidget(toggle)
    row.addStretch()
    caption = QLabel("Смены · Расходы · Отчёты")
    caption.setObjectName("smartreportCaption")
    row.addWidget(caption)
    return panel
