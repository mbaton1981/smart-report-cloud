"""Preview the generated PDF bytes; saving and printing never recalculate payroll."""
import logging
from pathlib import Path
import shutil
import tempfile

from PyQt6.QtCore import QRectF, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QImage, QPageLayout, QPainter
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QMessageBox, QPushButton

from document_preview import DocumentGalleryDialog

logger = logging.getLogger(__name__)


class PayrollPdfPreviewDialog(DocumentGalleryDialog):
    def __init__(self, source, suggested_path, parent=None):
        self.source = Path(source)
        self.suggested_path = Path(suggested_path)
        self._ready = False
        try:
            super().__init__([self.source], "Предпросмотр зарплатного PDF", parent)
        except (ImportError, OSError, ValueError, RuntimeError):
            if hasattr(self, "_pdf_documents"):
                self.release_resources()
            raise
        if not self._pages:
            self.release_resources()
            raise ValueError("В сформированном PDF нет страниц.")
        self._ready = True
        actions = QHBoxLayout()
        for label, callback in (("Сохранить как…", self.save_as), ("Печать", self.print_pdf)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        self.layout().insertLayout(2, actions)

    def show_page(self, index):
        try:
            super().show_page(index)
        except (OSError, ValueError, RuntimeError) as error:
            if not self._ready:
                raise
            logger.exception("Salary PDF page rendering failed")
            QMessageBox.critical(self, "Предпросмотр PDF", f"Не удалось показать страницу:\n{error}")

    def save_as(self):
        selected, _ = QFileDialog.getSaveFileName(self, "Сохранить зарплатный PDF",
                                                  str(self.suggested_path), "PDF (*.pdf)")
        if not selected:
            return
        target = Path(selected).resolve()
        if not target.suffix:
            target = target.with_suffix(".pdf")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source, target)
        except OSError as error:
            logger.exception("Salary PDF preview Save As failed; destination=%s", target)
            QMessageBox.critical(self, "Ошибка сохранения PDF", f"Не удалось сохранить файл:\n{target}\n{error}")
            return
        logger.info("Salary preview saved: %s", target)
        QMessageBox.information(self, "PDF сохранён", f"Файл сохранён:\n{target}")

    def print_pdf(self):
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setResolution(300)
        document = self._pdf_documents[0]
        printer.setPageOrientation(QPageLayout.Orientation.Landscape
                                   if document[0].get_width() > document[0].get_height()
                                   else QPageLayout.Orientation.Portrait)
        dialog = QPrintDialog(printer, self)
        dialog.setMinMax(1, len(document))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.print_document(printer)
        except (OSError, ValueError, RuntimeError) as error:
            logger.exception("Salary PDF printing failed")
            QMessageBox.critical(self, "Ошибка печати", f"Не удалось напечатать PDF:\n{error}")

    def print_document(self, printer):
        document = self._pdf_documents[0]
        first = max(1, printer.fromPage() or 1)
        last = min(len(document), printer.toPage() or len(document))
        if first > last:
            raise ValueError("Некорректный диапазон страниц.")
        painter = QPainter()
        if not painter.begin(printer):
            raise RuntimeError("Принтер недоступен или не удалось начать печать.")
        try:
            for index in range(first-1, last):
                if index != first-1 and not printer.newPage():
                    raise RuntimeError("Не удалось напечатать следующую страницу.")
                page = document[index]
                try:
                    bitmap = page.render(scale=printer.resolution()/72)
                    try:
                        image = bitmap.to_pil().convert("RGB")
                        qimage = QImage(image.tobytes(), image.width, image.height, image.width*3,
                                        QImage.Format.Format_RGB888).copy()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                viewport = painter.viewport()
                size = qimage.size().scaled(viewport.size(), Qt.AspectRatioMode.KeepAspectRatio)
                target = QRectF((viewport.width()-size.width())/2, (viewport.height()-size.height())/2,
                                size.width(), size.height())
                painter.drawImage(target, qimage)
            if printer.printerState() in (QPrinter.PrinterState.Error, QPrinter.PrinterState.Aborted):
                raise RuntimeError("Печать прервана или принтер сообщил об ошибке.")
        finally:
            painter.end()


def open_preview(source, suggested_path, parent):
    try:
        dialog = PayrollPdfPreviewDialog(source, suggested_path, parent)
    except (ImportError, OSError, ValueError, RuntimeError) as error:
        logger.exception("Embedded salary PDF preview unavailable; using system viewer")
        # The external viewer may load lazily or stay open after SmartReport exits.
        # Keep its separate temporary file; never give it a file deleted on dialog close.
        with tempfile.NamedTemporaryFile(prefix="SmartReport-payroll-", suffix=".pdf", delete=False) as temporary:
            external = Path(temporary.name)
        try:
            shutil.copyfile(source, external)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(external))):
                raise OSError("Системная программа просмотра PDF не найдена или не открыла файл.")
        except (OSError, RuntimeError) as fallback_error:
            external.unlink(missing_ok=True)
            raise RuntimeError(f"Встроенный просмотр недоступен: {error}\n"
                               f"Не удалось открыть PDF в системной программе: {fallback_error}") from fallback_error
        logger.info("Salary PDF opened in system viewer; retained temporary file=%s", external)
        return
    try:
        dialog.exec()
    finally:
        dialog.release_resources()
