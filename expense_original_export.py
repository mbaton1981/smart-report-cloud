"""Export copies of expense originals as files, independent of invoice status."""

from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from PyQt6.QtCore import QIODevice, QSaveFile


def build_original_export(documents, directory, download, *, archive, progress):
    # The download helper keys its result by client ID. Give each row a
    # temporary key so drafts and server records cannot collide in the export.
    download_rows = [dict(document, client_id=f"copy-{index}") for index, document in enumerate(documents, 1)]
    sources, errors = download(download_rows, directory, progress)
    files, missing = [], []
    for index, document in enumerate(download_rows, 1):
        key = document["client_id"]
        label = " · ".join(str(value) for value in (
            document.get("document_date"),
            document.get("supplier_name"),
            document.get("invoice_number"),
        ) if value) or str(index)
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label)[:96].strip(" .")
        paths = sources.get(key, [])
        if key in errors or not paths:
            missing.append(label)
        for part, path in enumerate(paths, 1):
            path = Path(path)
            suffix = path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", path.suffix) else ""
            part_label = f" ({part})" if len(paths) > 1 else ""
            name = f"Документ {stem}{part_label}{suffix}"
            files.append((path, name, f"{index:03d} - {name}"))
    if not files:
        return None, "", 0, missing
    if len(files) == 1 and not archive and not missing:
        path, name, _arcname = files[0]
        return path, name, 1, missing
    archive_path = Path(directory) / "expense-originals.zip"
    progress("Собираем архив оригиналов…")
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as output:
        for path, _name, arcname in files:
            output.write(path, arcname)
    return archive_path, "Копии документов расходов.zip", len(files), missing


def save_original_copy(source, target):
    if Path(source).resolve() == Path(target).resolve():
        raise ValueError("Выберите другой путь для копии документа.")
    output = QSaveFile(str(target))
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    try:
        with Path(source).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if output.write(chunk) != len(chunk):
                    raise OSError(output.errorString())
    except (OSError, ValueError):
        output.cancelWriting()
        raise
    if not output.commit():
        raise OSError(output.errorString())
