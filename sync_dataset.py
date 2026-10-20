"""Opt-in boundary for an explicitly prepared clean database pair."""
import sqlite3
from contextlib import closing
from pathlib import Path

HEADER = "X-SmartReport-Dataset"


def dataset_id(connection):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_dataset'"
    ).fetchone():
        return None
    rows = connection.execute("SELECT dataset_id FROM sync_dataset WHERE id=1").fetchall()
    if len(rows) != 1 or not rows[0][0]:
        raise ValueError("Некорректная граница набора данных; синхронизация остановлена.")
    return rows[0][0]


def file_dataset(path):
    path = Path(path).resolve()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return dataset_id(connection)


def require_response_dataset(response, expected):
    if expected and response.headers.get(HEADER) != expected:
        raise RuntimeError(
            "Сервер относится к другому или прежнему набору данных. "
            "Синхронизация остановлена: используйте согласованную базу и сервер обновления."
        )
