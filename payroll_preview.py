"""Open PDFs in a dialog owned by the SmartReport screen that launched them."""
import logging
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QPointF, QRectF, QSaveFile, Qt
from PyQt6.QtGui import QImage, QPageLayout, QPainter
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

logger = logging.getLogger(__name__)


class PayrollPdfPreviewDialog(QDialog):
    def __init__(self, source, suggested_path, parent=None):
        from PyQt6.QtPdf import QPdfDocument
        from PyQt6.QtPdfWidgets import QPdfView
        import pypdfium2 as pdfium

        super().__init__(parent)
        self._source_bytes = source if isinstance(source, bytes) else None
        self.source = Path("employee-data.pdf") if self._source_bytes is not None else Path(source)
        self.suggested_path = Path(suggested_path)
        self.setWindowTitle("Предпросмотр зарплатного PDF")
        self.resize(1180, 900)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.document = QPdfDocument(self)
        self._buffer = QBuffer(self)
        self._buffer.setData(self._source_bytes if self._source_bytes is not None else self.source.read_bytes())
        self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.document.load(self._buffer)
        error = self.document.error()
        if error != QPdfDocument.Error.None_:
            self.document.close()
            self._buffer.close()
            raise ValueError(f"Не удалось прочитать PDF: {error.name}")
        try:
            self._pdf_documents = [pdfium.PdfDocument(self._source_bytes if self._source_bytes is not None else str(self.source))]
        except (OSError, ValueError, RuntimeError):
            self.document.close()
            raise
        self._pages = list(range(self.document.pageCount()))
        self._page_index = 0
        if not self._pages:
            self.release_resources()
            raise ValueError("В сформированном PDF нет страниц.")
        layout = QVBoxLayout(self)
        navigation = QHBoxLayout()
        self.previous_button = QPushButton("‹")
        self.next_button = QPushButton("›")
        self.previous_button.clicked.connect(lambda: self.show_page(self._page_index-1))
        self.next_button.clicked.connect(lambda: self.show_page(self._page_index+1))
        self.page_label = QLabel()
        for widget in (self.previous_button, self.page_label, self.next_button):
            navigation.addWidget(widget)
        navigation.addStretch()
        layout.addLayout(navigation)
        self.preview = QPdfView(self)
        self.preview.setDocument(self.document)
        self.preview.setPageMode(QPdfView.PageMode.MultiPage)
        self.preview.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.preview.pageNavigator().currentPageChanged.connect(self.update_page)
        zoom_controls = QHBoxLayout()
        for label, callback in (("−", lambda: self.zoom(1/1.2)),
                                ("По ширине", lambda: self.preview.setZoomMode(QPdfView.ZoomMode.FitToWidth)),
                                ("100%", lambda: self.set_zoom(1)),
                                ("+", lambda: self.zoom(1.2))):
            button = QPushButton(label)
            button.clicked.connect(callback)
            zoom_controls.addWidget(button)
        zoom_controls.addStretch()
        layout.addLayout(zoom_controls)
        actions = QHBoxLayout()
        for label, callback in (("Сохранить как…", self.save_as), ("Печать", self.print_pdf)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.reject)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        layout.addWidget(self.preview, 1)
        self.finished.connect(self.release_resources)
        self.update_page(0)

    def set_zoom(self, value):
        self.preview.setZoomMode(self.preview.ZoomMode.Custom)
        self.preview.setZoomFactor(max(.1, min(8, value)))

    def zoom(self, factor):
        self.set_zoom(self.preview.zoomFactor()*factor)

    def update_page(self, index):
        self._page_index = index
        self.page_label.setText(f"{index+1} / {len(self._pages)} · {self.source.name}")
        self.previous_button.setEnabled(index > 0)
        self.next_button.setEnabled(index+1 < len(self._pages))

    def show_page(self, index):
        index = max(0, min(index, len(self._pages)-1))
        self.preview.pageNavigator().jump(index, QPointF(), self.preview.zoomFactor())
        self.update_page(index)

    def release_resources(self, _result=None):
        if hasattr(self, "preview"):
            self.preview.setDocument(None)
        self.document.close()
        self._buffer.close()
        for document in self._pdf_documents:
            document.close()
        self._pdf_documents.clear()

    def save_as(self):
        selected, _ = QFileDialog.getSaveFileName(self, "Сохранить PDF",
                                                  str(self.suggested_path), "PDF (*.pdf)")
        if not selected:
            return
        target = Path(selected).resolve()
        if not target.suffix:
            target = target.with_suffix(".pdf")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if self._source_bytes is not None or self.source.resolve() != target:
                data = self._source_bytes if self._source_bytes is not None else self.source.read_bytes()
                output = QSaveFile(str(target))
                if not output.open(QIODevice.OpenModeFlag.WriteOnly):
                    raise OSError(output.errorString())
                if output.write(data) != len(data):
                    output.cancelWriting()
                    raise OSError(output.errorString() or "Не удалось записать PDF полностью.")
                if not output.commit():
                    raise OSError(output.errorString())
        except OSError as error:
            logger.exception("PDF preview Save As failed; destination=%s", target)
            QMessageBox.critical(self, "Ошибка сохранения PDF", f"Не удалось сохранить файл:\n{target}\n{error}")
            return
        logger.info("PDF preview saved: %s", target)
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
            logger.exception("PDF preview printing failed")
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


def open_preview(source, suggested_path, parent, *, title=None):
    dialog = PayrollPdfPreviewDialog(source, suggested_path, parent)
    try:
        if title:
            dialog.setWindowTitle(title)
        dialog.exec()
    finally:
        dialog.release_resources()
        dialog.deleteLater()
        if parent is not None and parent.window().isVisible():
            parent.window().activateWindow()


def open_saved_pdf(path, parent, *, title="Просмотр PDF"):
    path = Path(path).resolve()
    try:
        open_preview(path, path, parent, title=title)
    except (ImportError, OSError, ValueError, RuntimeError) as error:
        logger.exception("Could not preview PDF: %s", path)
        QMessageBox.warning(parent, "Просмотр PDF", f"Не удалось открыть PDF:\n{path}\n{error}")
