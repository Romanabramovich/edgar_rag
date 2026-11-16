"""Idempotent XBRL fact snapshots with precise value text."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from ..sec import Fact
from .database import DEFAULT_RESERVE_BYTES, require_space, utc_now


def _fact_row(fact: Fact) -> tuple:
    payload = asdict(fact)
    value_text = json.dumps(fact.value, allow_nan=False)
    key = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return (digest, fact.ticker, fact.accession, fact.form, fact.taxonomy,
            fact.concept, fact.label, fact.unit, value_text, fact.start, fact.end,
            fact.filed, fact.fiscal_year, fact.fiscal_period, fact.frame)


def save_facts(connection: sqlite3.Connection, ticker: str, facts: list[Fact],
               *, reserve_bytes: int = DEFAULT_RESERVE_BYTES,
               disk_path: Path | None = None, disk_usage=shutil.disk_usage) -> int:
    rows = [_fact_row(f) for f in facts]
    if not rows and connection.execute("SELECT 1 FROM facts WHERE ticker=? LIMIT 1", (ticker,)).fetchone():
        raise ValueError("Refusing to replace nonempty SEC facts with an empty result")
    estimated = sum(sum(len(str(v).encode("utf-8")) for v in row if v is not None) for row in rows)
    require_space(disk_path or Path(connection.execute("PRAGMA database_list").fetchone()[2]).parent,
                  reserve_bytes, estimated, disk_usage)
    with connection:
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS current_fact_hashes(fact_hash TEXT PRIMARY KEY)")
        connection.execute("DELETE FROM current_fact_hashes")
        connection.executemany("INSERT OR IGNORE INTO current_fact_hashes VALUES(?)",
                               ((row[0],) for row in rows))
        before = connection.total_changes
        connection.executemany("INSERT OR IGNORE INTO facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        inserted = connection.total_changes - before
        connection.execute(
            "DELETE FROM facts WHERE ticker=? AND fact_hash NOT IN (SELECT fact_hash FROM current_fact_hashes)",
            (ticker,),
        )
        connection.execute("UPDATE companies SET facts_refreshed_at=? WHERE ticker=?", (utc_now(), ticker))
    return inserted
