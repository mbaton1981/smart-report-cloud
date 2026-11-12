"""Desktop appearance only; no database, authorization or document changes."""
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
import re

from PyQt6.QtCore import QEvent, QIODevice, QObject, QPointF, QRectF, QSaveFile, QSignalBlocker, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFontDatabase, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QBoxLayout, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget,
)

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
    "#4a3b24": "#fff0cf", "#26394b": "#e4f2ff", "#30343b": "#edf0f3",
    "#276749": "#4f705c", "#38a169": "#61836b", "#c6f6d5": "#8fb19a",
    "#68d391": "#376b4b", "#f6ad55": "#885c21", "#fca5a5": "#9c3e46",
    "#cbd5e1": "#52697b", "#cbd5e0": "#b9cad7",
    "#24445f": "#dcecf8", "#254c39": "#dcf1e4",
    "#dbeafe": "#16314a", "#d5f5e3": "#12351f",
}
BACKGROUND_ROLE = int(Qt.ItemDataRole.UserRole) + 271
FOREGROUND_ROLE = BACKGROUND_ROLE + 1
SHIFT_CONFIRMED_BG, SHIFT_CONFIRMED_FG = "#24445f", "#dbeafe"
SHIFT_INVOICED_BG, SHIFT_INVOICED_FG = "#254c39", "#d5f5e3"
# Цвет выделенной строки статуса: тёмная тема (фон, текст), светлая тема (фон, текст).
STATUS_SELECTION = {
    SHIFT_CONFIRMED_BG: (("#3a6ea5", "#ffffff"), ("#7fb0d8", "#0f2638")),
    SHIFT_INVOICED_BG: (("#2f7a54", "#ffffff"), ("#86c7a1", "#0f2a1a")),
}


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


class AdaptiveBoxLayout(QObject):
    """Stack a dense horizontal control row when its owning panel gets narrow."""

    def __init__(self, owner, layout, compact_width):
        super().__init__(owner)
        self.owner = owner
        self.layout = layout
        self.compact_width = compact_width
        self.wide_direction = layout.direction()
        if self.wide_direction in (QBoxLayout.Direction.LeftToRight, QBoxLayout.Direction.RightToLeft):
            self.compact_direction = QBoxLayout.Direction.TopToBottom
            owner.installEventFilter(self)
            self.update_direction()

    def eventFilter(self, watched, event):
        if watched is self.owner and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.update_direction()
        return False

    def update_direction(self):
        if self.owner.width() <= 0:
            return
        direction = (
            self.compact_direction if self.owner.width() < self.compact_width
            else self.wide_direction
        )
        if self.layout.direction() != direction:
            self.layout.setDirection(direction)


def adapt_box_layout(owner, layout, compact_width=900):
    controller = AdaptiveBoxLayout(owner, layout, compact_width)
    if not hasattr(owner, "_smartreport_responsive_layouts"):
        owner._smartreport_responsive_layouts = []
    owner._smartreport_responsive_layouts.append(controller)
    return controller


class AdaptiveSplitter(QObject):
    def __init__(self, owner, splitter, compact_width=1000, wide_sizes=(650, 520), compact_sizes=(420, 420)):
        super().__init__(owner)
        self.owner = owner
        self.splitter = splitter
        self.compact_width = compact_width
        self.wide_sizes = list(wide_sizes)
        self.compact_sizes = list(compact_sizes)
        self.compact = None
        owner.installEventFilter(self)
        self.update_orientation()

    def eventFilter(self, watched, event):
        if watched is self.owner and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.update_orientation()
        return False

    def update_orientation(self):
        if self.owner.width() <= 0:
            return
        compact = self.owner.width() < self.compact_width
        if compact == self.compact:
            return
        self.compact = compact
        self.splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        self.splitter.setSizes(self.compact_sizes if compact else self.wide_sizes)


def adapt_splitter(owner, splitter, compact_width=1000, wide_sizes=(650, 520), compact_sizes=(420, 420)):
    controller = AdaptiveSplitter(owner, splitter, compact_width, wide_sizes, compact_sizes)
    if not hasattr(owner, "_smartreport_responsive_splitters"):
        owner._smartreport_responsive_splitters = []
    owner._smartreport_responsive_splitters.append(controller)
    return controller


class DialogBoundsGuard(QObject):
    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Show or not isinstance(watched, QDialog):
            return False
        screen = watched.screen() or QApplication.primaryScreen()
        if screen is None:
            return False
        available = screen.availableGeometry()
        frame = watched.frameGeometry()
        width_extra = max(0, frame.width() - watched.width())
        height_extra = max(0, frame.height() - watched.height())
        max_width = max(1, available.width() - width_extra)
        max_height = max(1, available.height() - height_extra)
        minimum = watched.minimumSize()
        if minimum.width() > max_width or minimum.height() > max_height:
            watched.setMinimumSize(
                min(minimum.width(), max_width),
                min(minimum.height(), max_height),
            )
        width = min(max(watched.width(), watched.minimumWidth()), max_width)
        height = min(max(watched.height(), watched.minimumHeight()), max_height)
        watched.resize(width, height)
        frame = watched.frameGeometry()
        left = min(max(frame.left(), available.left()), max(available.left(), available.right() - frame.width() + 1))
        top = min(max(frame.top(), available.top()), max(available.top(), available.bottom() - frame.height() + 1))
        if left != frame.left() or top != frame.top():
            watched.move(left, top)
        return False


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
    table = item.tableWidget()
    if table is not None:
        register_table_color_item(table, item)


class StatusRowDelegate(QStyledItemDelegate):
    """Выделенная строка статуса остаётся читаемой и отличается от обычной окраски статуса."""

    _override = None

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if self._override is None:
            return
        background, foreground = (QColor(value) for value in self._override)
        option.backgroundBrush = QBrush(background)
        palette = QPalette(option.palette)
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette.setColor(group, QPalette.ColorRole.Text, foreground)
        option.palette = palette

    def paint(self, painter, option, index):
        status = index.data(BACKGROUND_ROLE)
        selection = STATUS_SELECTION.get(str(status).lower()) if status else None
        if selection is None or not option.state & QStyle.StateFlag.State_Selected:
            super().paint(painter, option, index)
            return
        # Без State_Selected общий стиль таблицы не перекрывает цвет выделения статуса.
        option = QStyleOptionViewItem(option)
        option.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
        self._override = selection[current_theme() == "light"]
        try:
            super().paint(painter, option, index)
        finally:
            self._override = None


def register_table_color_item(table, item):
    if item.tableWidget() is not table:
        return
    cells = set(getattr(table, "_smartreport_color_cells", ()))
    cells.add((item.row(), item.column()))
    table._smartreport_color_cells = cells
    manager = getattr(QApplication.instance(), "_smartreport_theme", None)
    if manager is not None:
        manager.register_color_table(table)


def refresh_table_item_colors(table):
    cells = set()
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None and (
                item.data(BACKGROUND_ROLE) or item.data(FOREGROUND_ROLE)
            ):
                cells.add((row, column))
    table._smartreport_color_cells = cells
    manager = getattr(QApplication.instance(), "_smartreport_theme", None)
    if manager is not None:
        manager.register_color_table(table)


def _canvas(size, scale=3):
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pixmap, painter


def _logo():
    pixmap, painter = _canvas(32)
    gradient = QLinearGradient(0, 0, 32, 32)
    gradient.setColorAt(0, QColor("#4f86b5"))
    gradient.setColorAt(1, QColor("#36628a"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(QRectF(0, 0, 32, 32), 8, 8)
    painter.setBrush(QColor("#f3f8fc"))
    for x, y, height in ((8, 18.5, 6.5), (14.25, 13, 12), (20.5, 7.5, 17.5)):
        painter.drawRoundedRect(QRectF(x, y, 3.5, height), 1.5, 1.5)
    painter.end()
    return pixmap


def line_icon(name, stroke, size=18):
    """Simple stroked icons in one style for tabs and the theme switch."""
    pixmap, painter = _canvas(size)
    painter.scale(size / 24, size / 24)
    pen = QPen(QColor(stroke), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    line = lambda *points: painter.drawLine(QPointF(*points[:2]), QPointF(*points[2:]))
    if name == "hours":
        painter.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        line(12, 7.5, 12, 12)
        line(12, 12, 15.5, 14)
    elif name == "salary":
        painter.drawRoundedRect(QRectF(3.5, 6.5, 17, 12), 2.5, 2.5)
        line(3.5, 10, 20.5, 10)
        line(14.5, 14.5, 17, 14.5)
    elif name == "balance":
        line(12, 4.5, 12, 19.5)
        line(8, 19.5, 16, 19.5)
        line(5, 7.5, 19, 7.5)
        for x in (5, 19):
            path = QPainterPath(QPointF(x - 3, 13.5))
            path.lineTo(x, 7.5)
            path.lineTo(x + 3, 13.5)
            path.arcTo(QRectF(x - 3, 11, 6, 5), 180, 180)
            painter.drawPath(path)
    elif name == "expenses":
        path = QPainterPath(QPointF(6, 3.5))
        path.lineTo(18, 3.5)
        path.lineTo(18, 20.5)
        for index, x in enumerate((16, 14, 12, 10, 8, 6)):
            path.lineTo(x, 19 if index % 2 == 0 else 20.5)
        path.closeSubpath()
        painter.drawPath(path)
        line(9, 8.5, 15, 8.5)
        line(9, 12.5, 15, 12.5)
    elif name == "reports":
        path = QPainterPath(QPointF(6, 3.5))
        path.lineTo(14, 3.5)
        path.lineTo(18.5, 8)
        path.lineTo(18.5, 20.5)
        path.lineTo(6, 20.5)
        path.closeSubpath()
        painter.drawPath(path)
        line(14, 3.5, 14, 8)
        line(14, 8, 18.5, 8)
        line(9, 13, 15.5, 13)
        line(9, 16.5, 13.5, 16.5)
    elif name == "catalogs":
        painter.drawRoundedRect(QRectF(5, 3.5, 14, 17), 2, 2)
        line(8.5, 3.5, 8.5, 20.5)
        line(11.5, 8.5, 16, 8.5)
        line(11.5, 12, 16, 12)
    elif name == "objects":
        path = QPainterPath(QPointF(4, 10))
        path.lineTo(12, 4)
        path.lineTo(20, 10)
        path.lineTo(20, 20)
        path.lineTo(4, 20)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawRect(QRectF(9, 13, 6, 7))
    elif name == "analytics":
        line(4, 20, 20, 20)
        for x, top in ((7, 13), (12, 8), (17, 4.5)):
            line(x, 17, x, top)
    elif name == "sun":
        painter.drawEllipse(QPointF(12, 12), 4, 4)
        for index in range(8):
            painter.save()
            painter.translate(12, 12)
            painter.rotate(index * 45)
            line(0, -7, 0, -9)
            painter.restore()
    elif name == "moon":
        path = QPainterPath()
        path.addEllipse(QPointF(12, 12), 8, 8)
        cut = QPainterPath()
        cut.addEllipse(QPointF(16.5, 8), 7, 7)
        painter.drawPath(path.subtracted(cut))
    painter.end()
    return QIcon(pixmap)


TAB_ICON_COLORS = {"dark": "#9fbbd3", "light": "#4a7398"}


def install_tab_icons(tabs, names):
    """names: tab index -> icon name; icons follow the current theme."""
    manager = getattr(QApplication.instance(), "_smartreport_theme", None)
    tabs.setIconSize(QSize(18, 18))

    def refresh(theme_name):
        for index, name in names.items():
            if index < tabs.count():
                tabs.setTabIcon(index, line_icon(name, TAB_ICON_COLORS.get(theme_name, TAB_ICON_COLORS["dark"])))

    refresh(current_theme())
    if manager is not None:
        manager.changed.connect(refresh)
        tabs.destroyed.connect(lambda *_: manager.changed.disconnect(refresh))


EXTRA = """
QMenu, QComboBox QAbstractItemView, QCalendarWidget QAbstractItemView {
    background-color: #28303d; color: #e2e8f0;
    selection-background-color: #2b6cb0; selection-color: #ffffff;
}
QCalendarWidget QAbstractItemView::item { background-color: #28303d; }
QCalendarWidget QAbstractItemView::item:selected { background-color: #2b6cb0; }
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
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {
    min-height: 32px;
}
QDateEdit { min-width: 126px; max-width: 168px; }
QToolButton {
    background-color: #313c4e; color: #ffffff; border: 1px solid #4a5568;
    padding: 7px 12px; border-radius: 7px; min-height: 20px; font-weight: 600;
}
QToolButton:hover { background-color: #3c495f; border-color: #4299e1; }
QToolButton:pressed { background-color: #2b6cb0; border-color: #3182ce; }
QToolButton:disabled { background-color: #252b35; color: #718096; border-color: #323946; }
QMenu { border: 1px solid #3f4c60; padding: 4px; }
QMenu::item { padding: 7px 24px 7px 10px; border-radius: 4px; }
QCalendarWidget QToolButton {
    background-color: #313c4e; border: 1px solid #4a5568; padding: 5px 8px;
    min-height: 24px; border-radius: 5px;
}
QCalendarWidget QToolButton:hover { background-color: #3c495f; border-color: #4299e1; }
QCalendarWidget QAbstractItemView { font-size: 13px; selection-background-color: #2b6cb0; }
QCalendarWidget QAbstractItemView::item { padding: 4px; min-width: 28px; min-height: 26px; }
QHeaderView::section { min-height: 32px; padding: 7px 8px; font-size: 13px; }
QTableWidget::item, QTableView::item { padding: 6px 8px; }
QSplitter::handle { background-color: #323946; }
QSplitter::handle:hover { background-color: #4299e1; }
QGroupBox { padding: 14px 10px 10px; }
QGroupBox::title { font-weight: 600; }
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
QFrame#smartreportHeader { background-color: #252b34; border: 1px solid #2f3642; border-radius: 10px; }
QPushButton#themeToggle {
    background-color: #2c3440; color: #d5dee8; border: 1px solid #3a4452;
    border-radius: 15px; padding: 5px 14px 5px 10px; font-weight: 600;
}
QPushButton#themeToggle:hover { background-color: #343e4c; border-color: #4f86b5; }
QTabBar::tab {
    background-color: transparent; color: #a3b1c2; border: none;
    border-bottom: 3px solid transparent; padding: 9px 16px 8px 12px; margin-right: 2px;
    font-weight: 600;
}
QTabBar::tab:hover { background-color: #2a313c; color: #e6edf5; border-radius: 6px; }
QTabBar::tab:selected {
    background-color: #2b3a4c; color: #ffffff; border-bottom: 3px solid #5a9bd5;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
}
QComboBox::down-arrow, QDateEdit::down-arrow {
    image: url("__DARK_CHEVRON__"); width: 12px; height: 8px; border: none;
}
QPushButton#successAction { background-color: #276749; color: #ffffff; border-color: #38a169; }
QPushButton#successAction:disabled { background-color: #252b35; color: #718096; border-color: #323946; }
QLabel#smartreportBrand { font-size: 18px; font-weight: 600; color: #eef3f8; }
QLabel#smartreportCaption { color: #8e9db0; font-size: 12px; }
"""
LIGHT_EXTRA = """
QFrame#smartreportHeader { background-color: #ffffff; border: 1px solid #dfe7ee; border-radius: 10px; }
QLabel#smartreportBrand { color: #1f3447; }
QLabel#smartreportCaption { color: #6d8193; }
QPushButton#themeToggle { background-color: #f2f6f9; color: #2c4052; border: 1px solid #d3dee7; }
QPushButton#themeToggle:hover { background-color: #e8f0f6; border-color: #4a7398; }
QTabBar::tab { background-color: transparent; color: #4f6577; border-bottom: 3px solid transparent; }
QTabBar::tab:hover { background-color: #e6edf3; color: #1f3447; }
QTabBar::tab:selected { background-color: #ffffff; color: #17324a; border-bottom: 3px solid #3d78ad; }
QPushButton#primaryAction { background-color: #52718a; color: #ffffff; border-color: #52718a; }
QPushButton#primaryAction:hover { background-color: #45647d; color: #ffffff; }
QPushButton#primaryAction:disabled { background-color: #e8edf1; color: #657888; border-color: #ccd8e1; }
QPushButton#successAction { background-color: #4f705c; color: #ffffff; border-color: #61836b; }
QPushButton#successAction:disabled { background-color: #e8edf1; color: #657888; border-color: #ccd8e1; }
QTableWidget::item:selected, QTableView::item:selected { background-color: #d6e5f0; color: #243b4e; }
QTableWidget::item:selected:!active, QTableView::item:selected:!active {
    background-color: #d6e5f0; color: #243b4e;
}
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextEdit,
QComboBox QAbstractItemView, QCalendarWidget QAbstractItemView {
    selection-background-color: #d6e5f0; selection-color: #243b4e;
    placeholder-text-color: #657888;
}
QCalendarWidget QAbstractItemView::item { background-color: #fcfdfe; }
QCalendarWidget QAbstractItemView::item:selected {
    background-color: #d6e5f0; color: #243b4e;
}
QMenu::item:selected { background-color: #d6e5f0; color: #243b4e; }
QToolButton { background-color: #e2ebf2; color: #2c4052; border-color: #b7cad9; }
QToolButton:hover { background-color: #d4e2ed; border-color: #7d9bb1; }
QToolButton:pressed { background-color: #52718a; color: #ffffff; border-color: #5c7e99; }
QToolButton:disabled { background-color: #e8edf1; color: #657888; border-color: #ccd8e1; }
QMenu { border-color: #b9cad7; }
QCalendarWidget QToolButton { background-color: #e2ebf2; color: #2c4052; border-color: #b7cad9; }
QCalendarWidget QToolButton:hover { background-color: #d4e2ed; border-color: #7d9bb1; }
QCalendarWidget QAbstractItemView { selection-background-color: #d6e5f0; }
QHeaderView::section { background-color: #e4ebf1; color: #395d77; border-color: #ccd8e1; }
QSplitter::handle { background-color: #dce5ec; }
QSplitter::handle:hover { background-color: #7d9bb1; }
QComboBox::down-arrow, QDateEdit::down-arrow {
    image: url("__CHEVRON__"); width: 12px; height: 8px; border: none;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("__UP__"); }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("__CHEVRON__"); }
"""


@contextmanager
def _paused_layout(application):
    """Смена стиля заставляет заголовки таблиц с ResizeToContents десятки раз мерить все ячейки.

    На время смены стиля замеры отключаются, а прежние режимы и ширины возвращаются без пересчёта.
    """
    headers = [(header, [header.sectionResizeMode(index) for index in range(header.count())])
               for header in application.allWidgets() if isinstance(header, QHeaderView)]
    windows = [window for window in application.topLevelWidgets() if window.isVisible() and window.updatesEnabled()]
    for header, modes in headers:
        for index, mode in enumerate(modes):
            if mode == QHeaderView.ResizeMode.ResizeToContents:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
    for window in windows:
        window.setUpdatesEnabled(False)
    try:
        yield
    finally:
        for header, modes in headers:
            for index, mode in enumerate(modes):
                if mode == QHeaderView.ResizeMode.ResizeToContents and index < header.count():
                    header.setSectionResizeMode(index, mode)
        for window in windows:
            window.setUpdatesEnabled(True)


class ThemeManager(QObject):
    changed = pyqtSignal(str)

    def __init__(self, application, dark_style, settings_path=None):
        super().__init__(application)
        self.application = application
        self.dark_style = dark_style
        self._dialog_bounds_guard = DialogBoundsGuard(self)
        application.installEventFilter(self._dialog_bounds_guard)
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
        with _paused_layout(self.application):
            self.application.setStyleSheet(self.stylesheet())
            self._recolor_widgets()
        self.changed.emit(self.name)

    def _recolor_widgets(self):
        from PyQt6.QtWidgets import QTableWidget
        brushes = {}
        for widget in self.application.allWidgets():
            inline = widget.property("smartreportDarkStyle")
            if inline is not None:
                _inline_style(widget, style_text(inline, self.name))
            if not isinstance(widget, QTableWidget):
                continue
            if not hasattr(widget, "_smartreport_color_cells"):
                refresh_table_item_colors(widget)
            cells = tuple(widget._smartreport_color_cells)
            if not cells:
                continue
            self.register_color_table(widget)
            was_enabled = widget.updatesEnabled()
            if was_enabled:
                widget.setUpdatesEnabled(False)
            try:
                with QSignalBlocker(widget):
                    for row, column in cells:
                        item = widget.item(row, column)
                        if item is None:
                            continue
                        for role, setter in (
                            (BACKGROUND_ROLE, item.setBackground),
                            (FOREGROUND_ROLE, item.setForeground),
                        ):
                            original = item.data(role)
                            if not original:
                                continue
                            value = COLORS.get(original.lower(), original) if self.name == "light" else original
                            brush = brushes.get(value)
                            if brush is None:
                                brush = brushes[value] = QBrush(QColor(value))
                            setter(brush)
            finally:
                if was_enabled:
                    widget.setUpdatesEnabled(True)
                    widget.viewport().update()

    def register_color_table(self, table):
        if getattr(table, "_smartreport_color_manager", None) is self:
            return
        model = table.model()
        for signal in (
            model.layoutChanged,
            model.rowsInserted,
            model.rowsRemoved,
            model.columnsInserted,
            model.columnsRemoved,
            model.modelReset,
        ):
            signal.connect(self._refresh_color_table)
        table._smartreport_color_manager = self

    def _refresh_color_table(self, *_):
        model = self.sender()
        for widget in self.application.allWidgets():
            if isinstance(widget, QTableWidget) and widget.model() is model:
                refresh_table_item_colors(widget)
                return

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
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(manager.toggle)
        manager.changed.connect(self.update_name)
        self.update_name(manager.name)

    def update_name(self, name):
        target = "light" if name == "dark" else "dark"
        self.setText("Светлая" if target == "light" else "Тёмная")
        self.setIcon(line_icon("sun" if target == "light" else "moon", "#f2c46d" if name == "dark" else "#4a7398", 16))
        self.setToolTip(
            ("Включить светлую тему" if target == "light" else "Включить тёмную тему")
            + ". Без перезапуска; выбор сохраняется для этого профиля."
        )


def header(parent=None):
    application = QApplication.instance()
    manager = ensure_theme(application.styleSheet())
    panel = QFrame(parent)
    panel.setObjectName("smartreportHeader")
    row = QHBoxLayout(panel)
    row.setContentsMargins(14, 8, 14, 8)
    row.setSpacing(10)
    icon = QLabel()
    icon.setPixmap(_logo())
    icon.setAccessibleName("Логотип SmartReport")
    row.addWidget(icon)
    brand = QLabel("SmartReport")
    brand.setObjectName("smartreportBrand")
    row.addWidget(brand)
    toggle = ThemeToggle(manager)
    row.addSpacing(12)
    row.addWidget(toggle)
    row.addStretch()
    caption = QLabel("Смены · Расходы · Отчёты")
    caption.setObjectName("smartreportCaption")
    row.addWidget(caption)
    return panel
