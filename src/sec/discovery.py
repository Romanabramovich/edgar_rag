"""Ticker resolution and complete SEC submissions-history discovery."""

from datetime import date
from typing import Iterator

from .client import SECClient
from .models import ACCESSION, DATA, FORMS, SAFE_FILE, TICKERS, WWW, Filing


def resolve_cik(client: SECClient, ticker: str) -> int:
    ticker = ticker.upper()
    if ticker not in TICKERS:
        raise ValueError(f"Ticker must be one of {', '.join(sorted(TICKERS))}")
    companies = client.get_json(f"{WWW}/files/company_tickers.json")
    for company in companies.values():
        if company.get("ticker", "").upper() == ticker:
            return int(company["cik_str"])
    raise LookupError(f"{ticker} missing from SEC ticker list")


def _rows(columns: dict) -> Iterator[dict]:
    if not isinstance(columns, dict):
        raise ValueError("Invalid SEC submission columns")
    count = len(columns.get("accessionNumber", []))
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    if any(len(columns.get(key, [])) != count for key in keys):
        raise ValueError("Misaligned SEC submission columns")
    for i in range(count):
        yield {key: columns[key][i] for key in keys}


def _filing(ticker: str, cik: int, row: dict) -> Filing | None:
    form = row["form"]
    if form not in FORMS:
        return None
    accession = row["accessionNumber"]
    document = row["primaryDocument"]
    if not ACCESSION.fullmatch(accession):
        raise ValueError(f"Invalid accession: {accession}")
    if document and (not SAFE_FILE.fullmatch(document) or document in {".", ".."}):
        raise ValueError(f"Invalid primary document: {document}")
    filed = row["filingDate"]
    date.fromisoformat(filed)
    report = row["reportDate"] or None
    if report:
        date.fromisoformat(report)
    base = f"{WWW}/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    archive = f"{WWW}/Archives/edgar/data/{cik}/{accession}.txt"
    return Filing(ticker, cik, accession, form, filed, report, document,
                  f"{base}/{accession}-index.html" if document else archive,
                  f"{base}/{document}" if document else archive)


def discover_filings(client: SECClient, ticker: str, cik: int | None = None) -> list[Filing]:
    ticker = ticker.upper()
    cik = resolve_cik(client, ticker) if cik is None else cik
    if ticker not in TICKERS or cik <= 0:
        raise ValueError("Invalid starter ticker or CIK")
    root = client.get_json(f"{DATA}/submissions/CIK{cik:010d}.json")
    histories = [root["filings"]["recent"]]
    for item in root["filings"].get("files", []):
        name = item["name"]
        if not SAFE_FILE.fullmatch(name) or not name.endswith(".json"):
            raise ValueError(f"Invalid submission history file: {name}")
        histories.append(client.get_json(f"{DATA}/submissions/{name}"))
    by_accession: dict[str, Filing] = {}
    for history in histories:
        for row in _rows(history):
            filing = _filing(ticker, cik, row)
            if filing is not None:
                by_accession.setdefault(filing.accession, filing)
    return sorted(by_accession.values(), key=lambda f: (f.filed, f.accession), reverse=True)
