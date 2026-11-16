"""Connection setup and the disk reserve shared by storage operations."""

from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
import shutil
import sqlite3


GIB = 1024 ** 3
DEFAULT_RESERVE_BYTES = 6 * GIB
WRITE_MARGIN_BYTES = 64 * 1024 ** 2


class DiskReserveError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_space(path: Path, reserve_bytes: int, estimated_bytes: int = 0,
                  disk_usage=shutil.disk_usage) -> None:
    """Leave headroom for SQLite journal, pages, and concurrent small writes."""
    if reserve_bytes < 0:
        raise ValueError("reserve_bytes cannot be negative")
    free = disk_usage(path).free
    needed = reserve_bytes + WRITE_MARGIN_BYTES + 4 * estimated_bytes
    if free < needed:
        raise DiskReserveError(
            f"Disk reserve reached: {free / GIB:.2f} GiB free, "
            f"need {needed / GIB:.2f} GiB before this write"
        )


def open_database(path: Path, *, reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                  disk_usage=shutil.disk_usage) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require_space(path.parent, reserve_bytes, disk_usage=disk_usage)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        schema = files("src.storage").joinpath("schema.sql").read_text(encoding="utf-8")
        connection.executescript(schema)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(filings)")}
        if "indexed_at" in columns and "stored_at" not in columns:
            connection.execute("ALTER TABLE filings RENAME COLUMN indexed_at TO stored_at")
        return connection
    except Exception:
        connection.close()
        raise
