import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)


class ZoomableDocumentView(QGraphicsView):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._image = self._scene.addPixmap(pixmap)
        self.setScene(self._scene)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMinimumSize(280, 320)
        QTimer.singleShot(0, self.fit_width)

    def zoom(self, factor):
        current = self.transform().m11()
        target = min(8.0, max(0.1, current * factor))
        self.scale(target / current, target / current)

    def fit_width(self):
        pixmap = self._image.pixmap()
        if pixmap.isNull() or pixmap.width() <= 0:
            return
        self.resetTransform()
        factor = max(0.1, (self.viewport().width() - 24) / pixmap.width())
        self.scale(factor, factor)
        self.centerOn(self._image)

    def show_original_size(self):
        self.resetTransform()
        self.centerOn(self._image)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)

class DocumentPreviewDialog(QDialog):
    def __init__(self, pixmap, title, parent=None, source_path=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1180, 900)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.preview = ZoomableDocumentView(pixmap, self)
        for label, callback in (
            ("−", lambda: self.preview.zoom(1 / 1.2)),
            ("По ширине", self.preview.fit_width),
            ("100%", self.preview.show_original_size),
            ("+", lambda: self.preview.zoom(1.2)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            controls.addWidget(button)
        if source_path:
            source_button = QPushButton("Открыть файл целиком")
            source_button.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(source_path)))
            )
            controls.addWidget(source_button)
        controls.addStretch()
        layout.addLayout(controls)
        layout.addWidget(self.preview, 1)

class DocumentGalleryDialog(QDialog):
    def __init__(self, paths, title, parent=None, initial_page=0, initial_attachment=0):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1180, 900)
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="smartreport_document_gallery_")
        self._pdf_documents = []
        self._pages = []
        self._attachment_offsets = []
        self._page_index = 0
        self.finished.connect(self.release_resources)
        starting_page = 0
        for attachment_index, path in enumerate(paths):
            self._attachment_offsets.append(len(self._pages))
            if attachment_index == initial_attachment:
                starting_page = len(self._pages) + initial_page
            source = Path(path)
            if source.suffix.casefold() == ".pdf":
                import pypdfium2 as pdfium
                try:
                    document = pdfium.PdfDocument(str(source))
                except Exception:
                    for opened_document in self._pdf_documents:
                        opened_document.close()
                    self._temporary_directory.cleanup()
                    raise
                self._pdf_documents.append(document)
                self._pages.extend(("pdf", document, page, source.name) for page in range(len(document)))
            else:
                self._pages.append(("image", source, 0, source.name))

        layout = QVBoxLayout(self)
        self.preview = ZoomableDocumentView(QPixmap(), self)
        controls = QHBoxLayout()
        self.previous_button = QPushButton("‹")
        self.previous_button.setToolTip("Предыдущая страница")
        self.previous_button.clicked.connect(lambda: self.show_page(self._page_index - 1))
        self.next_button = QPushButton("›")
        self.next_button.setToolTip("Следующая страница")
        self.next_button.clicked.connect(lambda: self.show_page(self._page_index + 1))
        self.page_label = QLabel("")
        self.page_label.setMinimumWidth(150)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.page_label)
        controls.addWidget(self.next_button)
        controls.addStretch()
        layout.addLayout(controls)
        zoom_controls = QHBoxLayout()
        for label, callback in (
            ("−", lambda: self.preview.zoom(1 / 1.2)),
            ("По ширине", self.preview.fit_width),
            ("100%", self.preview.show_original_size),
            ("+", lambda: self.preview.zoom(1.2)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            zoom_controls.addWidget(button)
        zoom_controls.addStretch()
        layout.addLayout(zoom_controls)
        layout.addWidget(self.preview, 1)
        self.show_page(starting_page)

    def show_page(self, index):
        if not self._pages:
            self.page_label.setText("Страниц нет")
            self.previous_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        self._page_index = max(0, min(int(index), len(self._pages) - 1))
        kind, source, page_index, name = self._pages[self._page_index]
        if kind == "pdf":
            image = source[page_index].render(scale=1.5).to_pil().convert("RGB")
            qimage = QImage(
                image.tobytes(), image.width, image.height, image.width * 3,
                QImage.Format.Format_RGB888,
            ).copy()
            pixmap = QPixmap.fromImage(qimage)
        else:
            pixmap = QPixmap(str(source))
        self.preview._image.setPixmap(pixmap)
        self.preview.fit_width()
        self.page_label.setText(f"{self._page_index + 1} / {len(self._pages)} · {name}")
        self.previous_button.setEnabled(self._page_index > 0)
        self.next_button.setEnabled(self._page_index + 1 < len(self._pages))

    def show_source(self, attachment_index, page):
        if 0 <= attachment_index < len(self._attachment_offsets):
            end = self._attachment_offsets[attachment_index + 1] if attachment_index + 1 < len(self._attachment_offsets) else len(self._pages)
            index = self._attachment_offsets[attachment_index] + max(0, page - 1)
            if index < end:
                self.show_page(index)

    def release_resources(self, _result=None):
        for document in self._pdf_documents:
            document.close()
        self._pdf_documents.clear()
        self._temporary_directory.cleanup()

    def closeEvent(self, event):
        self.release_resources()
        super().closeEvent(event)
