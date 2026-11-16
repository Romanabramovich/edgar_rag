"""Resumable, disk-aware SEC import into the local SQLite catalog."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

from .sec import SECClient, discover_filings, extract_facts, extract_filing, resolve_cik
from .storage import (
    DEFAULT_RESERVE_BYTES, GIB, DiskReserveError, coverage, is_stored,
    load_catalog, open_database, record_error, require_space, save_catalog,
    save_facts, save_filing,
)


ORDER = ("KO", "AAPL", "NVDA", "GOOG")


@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    errors: int = 0
    stopped_for_space: bool = False


def _years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def due(timestamp: str | None, *, now: datetime, refresh_hours: int) -> bool:
    if not timestamp:
        return True
    return datetime.fromisoformat(timestamp) <= now - timedelta(hours=refresh_hours)


def refresh_catalog(connection: sqlite3.Connection, client: SECClient, ticker: str,
                    *, now: datetime, refresh_hours: int, reserve_bytes: int,
                    disk_path: Path) -> list:
    current = connection.execute(
        "SELECT discovered_at,facts_refreshed_at,cik FROM companies WHERE ticker=?", (ticker,)
    ).fetchone()
    if current and not due(current[0], now=now, refresh_hours=refresh_hours):
        filings = load_catalog(connection, ticker)
        cik = current[2]
    else:
        require_space(disk_path, reserve_bytes)
        cik = resolve_cik(client, ticker)
        filings = discover_filings(client, ticker, cik)
        require_space(disk_path, reserve_bytes, estimated_bytes=len(filings) * 1000)
        save_catalog(connection, ticker, cik, filings, checked_at=now.isoformat(timespec="seconds"))
    # Fact refresh has its own checkpoint. A failed fact request leaves it due.
    if not current or due(current[1], now=now, refresh_hours=refresh_hours):
        facts = list(extract_facts(client, ticker, cik))
        save_facts(connection, ticker, facts, reserve_bytes=reserve_bytes, disk_path=disk_path)
    return filings


def planned_filings(catalogs: dict[str, list], *, today: date) -> list:
    five_years = _years_ago(today, 5).isoformat()
    ten_years = _years_ago(today, 10).isoformat()
    output = []
    seen = set()

    def add(filing):
        if filing.accession not in seen:
            seen.add(filing.accession)
            output.append(filing)

    # Give every company its recent window before historical backfill.
    for ticker in ORDER:
        for filing in catalogs.get(ticker, []):
            if filing.filed >= five_years:
                add(filing)
    # KO receives the first chance at full history, followed by AAPL.
    for ticker in ("KO", "AAPL"):
        for filing in catalogs.get(ticker, []):
            add(filing)
    for ticker in ("NVDA", "GOOG"):
        for filing in catalogs.get(ticker, []):
            if filing.filed >= ten_years:
                add(filing)
    return output


def import_filings(connection: sqlite3.Connection, client: SECClient,
                   *, db_path: Path, reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                   refresh_hours: int = 24, max_filings: int | None = None,
                   now: datetime | None = None, log=print) -> ImportReport:
    if max_filings is not None and max_filings < 1:
        raise ValueError("max_filings must be positive")
    now = now or datetime.now(timezone.utc)
    report = ImportReport()
    catalogs = {}
    for ticker in ORDER:
        try:
            catalogs[ticker] = refresh_catalog(
                connection, client, ticker, now=now, refresh_hours=refresh_hours,
                reserve_bytes=reserve_bytes, disk_path=db_path.parent,
            )
            log(f"{ticker}: {len(catalogs[ticker])} available filings")
        except DiskReserveError as exc:
            report.stopped_for_space = True
            log(str(exc))
            return report
        except Exception as exc:
            report.errors += 1
            log(f"{ticker}: catalog/facts refresh failed: {type(exc).__name__}: {exc}")
            catalogs[ticker] = load_catalog(connection, ticker)

    for filing in planned_filings(catalogs, today=now.date()):
        if is_stored(connection, filing.accession):
            report.skipped += 1
            continue
        if max_filings is not None and report.imported >= max_filings:
            break
        try:
            require_space(db_path.parent, reserve_bytes)
            text = extract_filing(client, filing)
            if save_filing(connection, filing, text, reserve_bytes=reserve_bytes,
                           disk_path=db_path.parent):
                report.imported += 1
                log(f"Imported {filing.ticker} {filing.form} {filing.filed} {filing.accession}")
        except DiskReserveError as exc:
            report.stopped_for_space = True
            log(str(exc))
            break
        except Exception as exc:
            report.errors += 1
            record_error(connection, filing.accession, exc)
            log(f"Failed {filing.accession}: {type(exc).__name__}: {exc}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import SEC filings into local SQLite")
    parser.add_argument("command", choices=("import", "coverage"))
    parser.add_argument("--db", type=Path, default=Path("data/edgar.sqlite"))
    parser.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"))
    parser.add_argument("--reserve-gib", type=float, default=6.0)
    parser.add_argument("--max-filings", type=int)
    parser.add_argument("--refresh-hours", type=int, default=24)
    args = parser.parse_args(argv)
    if args.reserve_gib < 6:
        parser.error("reserve must be at least 6 GiB")
    if args.refresh_hours < 1:
        parser.error("refresh-hours must be positive")
    if args.command == "import" and not args.user_agent:
        parser.error("Set --user-agent or SEC_USER_AGENT with a contact email")
    reserve = int(args.reserve_gib * GIB)
    try:
        connection = open_database(args.db, reserve_bytes=reserve)
        try:
            if args.command == "coverage":
                print(json.dumps(coverage(connection), indent=2))
                return 0
            report = import_filings(connection, SECClient(args.user_agent),
                                    db_path=args.db, reserve_bytes=reserve,
                                    refresh_hours=args.refresh_hours,
                                    max_filings=args.max_filings)
            print(json.dumps({"report": vars(report), "coverage": coverage(connection)}, indent=2))
            return 1 if report.errors else 0
        finally:
            connection.close()
    except DiskReserveError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
