"""Filing catalog, source passages, coverage, and import errors."""

from pathlib import Path
import shutil
import sqlite3

from ..sec import Filing
from .database import DEFAULT_RESERVE_BYTES, require_space, utc_now


def save_catalog(connection: sqlite3.Connection, ticker: str, cik: int,
                 filings: list[Filing], *, checked_at: str | None = None) -> None:
    checked_at = checked_at or utc_now()
    if not filings and connection.execute("SELECT 1 FROM filings WHERE ticker=? LIMIT 1", (ticker,)).fetchone():
        raise ValueError("Refusing to replace a nonempty SEC catalog with an empty result")
    with connection:
        connection.execute(
            "INSERT INTO companies(ticker,cik,discovered_at) VALUES(?,?,?) "
            "ON CONFLICT(ticker) DO UPDATE SET cik=excluded.cik, discovered_at=excluded.discovered_at",
            (ticker, cik, checked_at),
        )
        connection.executemany(
            "INSERT INTO filings(accession,ticker,cik,form,filed,report_date,primary_document,filing_url,source_url) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(accession) DO UPDATE SET "
            "form=excluded.form, filed=excluded.filed, report_date=excluded.report_date, "
            "primary_document=excluded.primary_document, filing_url=excluded.filing_url, source_url=excluded.source_url",
            [(f.accession, f.ticker, f.cik, f.form, f.filed, f.report_date,
              f.primary_document, f.filing_url, f.source_url) for f in filings],
        )
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS current_accessions(accession TEXT PRIMARY KEY)")
        connection.execute("DELETE FROM current_accessions")
        connection.executemany("INSERT OR IGNORE INTO current_accessions VALUES(?)",
                               ((f.accession,) for f in filings))
        connection.execute(
            "DELETE FROM filings WHERE ticker=? AND accession NOT IN (SELECT accession FROM current_accessions)",
            (ticker,),
        )


def load_catalog(connection: sqlite3.Connection, ticker: str) -> list[Filing]:
    rows = connection.execute(
        "SELECT ticker,cik,accession,form,filed,report_date,primary_document,filing_url,source_url "
        "FROM filings WHERE ticker=? ORDER BY filed DESC,accession DESC", (ticker,),
    )
    return [Filing(**dict(row)) for row in rows]


def is_stored(connection: sqlite3.Connection, accession: str) -> bool:
    row = connection.execute("SELECT stored_at FROM filings WHERE accession=?", (accession,)).fetchone()
    return row is not None and row[0] is not None


def split_passages(text: str, *, max_chars: int = 1800, overlap_chars: int = 200) -> list[str]:
    """Keep nearby context and preserve exact substrings for citation display."""
    if max_chars <= 0 or not 0 <= overlap_chars < max_chars:
        raise ValueError("Invalid passage size or overlap")
    if not text:
        return []
    passages = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = text.rfind("\n", start + max_chars // 2, end)
            if boundary > start:
                end = boundary + 1
        passages.append(text[start:end])
        if end == len(text):
            break
        start = max(start + 1, end - overlap_chars)
    return passages


def save_filing(connection: sqlite3.Connection, filing: Filing, text: str,
                *, reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                disk_path: Path | None = None, disk_usage=shutil.disk_usage) -> bool:
    if not text:
        raise ValueError("Filing text is empty")
    if is_stored(connection, filing.accession):
        return False
    passages = split_passages(text)
    estimated = len(text.encode("utf-8")) + sum(len(p.encode("utf-8")) for p in passages)
    require_space(disk_path or Path(connection.execute("PRAGMA database_list").fetchone()[2]).parent,
                  reserve_bytes, estimated, disk_usage)
    with connection:
        connection.execute("INSERT INTO filing_text(accession,body) VALUES(?,?)", (filing.accession, text))
        connection.executemany("INSERT INTO passages(accession,ordinal,body) VALUES(?,?,?)",
                               ((filing.accession, i, p) for i, p in enumerate(passages)))
        connection.execute("UPDATE filings SET stored_at=? WHERE accession=?", (utc_now(), filing.accession))
        connection.execute("DELETE FROM import_errors WHERE accession=?", (filing.accession,))
    return True


def record_error(connection: sqlite3.Connection, accession: str, error: Exception) -> None:
    with connection:
        connection.execute(
            "INSERT INTO import_errors(accession,attempts,last_error,last_attempt_at) VALUES(?,1,?,?) "
            "ON CONFLICT(accession) DO UPDATE SET attempts=attempts+1, "
            "last_error=excluded.last_error, last_attempt_at=excluded.last_attempt_at",
            (accession, f"{type(error).__name__}: {error}"[:1000], utc_now()),
        )


def coverage(connection: sqlite3.Connection) -> list[dict]:
    result = []
    for ticker in ("KO", "AAPL", "NVDA", "GOOG"):
        row = connection.execute(
            "SELECT COUNT(*) AS available_count, MIN(filed) AS available_from, MAX(filed) AS available_to, "
            "COUNT(stored_at) AS stored_count, "
            "MIN(CASE WHEN stored_at IS NOT NULL THEN filed END) AS stored_from, "
            "MAX(CASE WHEN stored_at IS NOT NULL THEN filed END) AS stored_to "
            "FROM filings WHERE ticker=?", (ticker,),
        ).fetchone()
        company = connection.execute(
            "SELECT discovered_at,facts_refreshed_at FROM companies WHERE ticker=?", (ticker,)
        ).fetchone()
        result.append({"ticker": ticker, **dict(row),
                       "discovered_at": company[0] if company else None,
                       "facts_refreshed_at": company[1] if company else None})
    return result
