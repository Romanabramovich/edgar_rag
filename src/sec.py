"""SEC discovery and extraction. Persistence is handled by a later stage."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import io
import json
import re
import sys
import time
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATA = "https://data.sec.gov"
WWW = "https://www.sec.gov"
FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
TICKERS = frozenset({"AAPL", "NVDA", "KO", "GOOG"})
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Filing:
    ticker: str
    cik: int
    accession: str
    form: str
    filed: str
    report_date: str | None
    primary_document: str
    filing_url: str
    source_url: str


@dataclass(frozen=True)
class Fact:
    ticker: str
    accession: str
    form: str
    taxonomy: str
    concept: str
    label: str
    unit: str
    value: int | float
    start: str | None
    end: str
    filed: str
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None


class SECClient:
    """One serial client with a shared request interval and bounded retries."""

    def __init__(
        self,
        user_agent: str,
        *,
        requests_per_second: float = 5.0,
        opener: Callable = urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = 50_000_000,
    ) -> None:
        if not user_agent.strip() or "@" not in user_agent:
            raise ValueError("Provide a descriptive SEC User-Agent with a contact email")
        if not 0 < requests_per_second <= 10:
            raise ValueError("SEC request rate must be between 0 and 10 per second")
        self.user_agent = user_agent
        self.interval = 1 / requests_per_second
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self.max_bytes = max_bytes
        self.next_request = 0.0

    def _wait_turn(self) -> None:
        now = self.clock()
        if now < self.next_request:
            self.sleep(self.next_request - now)
        self.next_request = self.clock() + self.interval

    def get_bytes(self, url: str) -> bytes:
        if not (url.startswith(DATA + "/") or url.startswith(WWW + "/")):
            raise ValueError("Only SEC HTTPS URLs are allowed")
        request = Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept-Encoding": "identity",
            "Accept": "application/json, text/html, text/plain, */*",
        })
        for attempt in range(4):
            self._wait_turn()
            try:
                with self.opener(request, timeout=45) as response:
                    body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise ValueError(f"SEC response exceeds {self.max_bytes} bytes: {url}")
                return body
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    try:
                        delay = max(0.0, (parsedate_to_datetime(retry_after).timestamp() - time.time()))
                    except (TypeError, ValueError, OverflowError):
                        delay = 0.0
                self.sleep(max(delay, 2.0 ** attempt))
            except URLError:
                if attempt == 3:
                    raise
                self.sleep(2.0 ** attempt)
        raise AssertionError("unreachable")

    def get_json(self, url: str) -> dict:
        return json.loads(self.get_bytes(url))


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


class _TextParser(HTMLParser):
    BREAKS = frozenset({"p", "div", "br", "hr", "tr", "table", "section", "article", "li", "h1", "h2", "h3", "h4"})
    SKIP = frozenset({"script", "style", "noscript", "svg", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP or self.skip_depth:
            self.skip_depth += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def parse_document(body: bytes) -> str:
    parser = _TextParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    text = "".join(parser.parts)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _archive_document(body: bytes, primary_document: str, form: str) -> bytes:
    decoded = body.decode("utf-8", errors="replace")
    fallback = None
    for block in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", decoded, flags=re.I | re.S):
        filename = re.search(r"<FILENAME>\s*([^\r\n<]+)", block, flags=re.I)
        kind = re.search(r"<TYPE>\s*([^\r\n<]+)", block, flags=re.I)
        match = re.search(r"<TEXT>\s*(.*?)(?:</TEXT>|\Z)", block, flags=re.I | re.S)
        if not match:
            continue
        document = match.group(1).encode("utf-8")
        if filename and filename.group(1).strip() == primary_document:
            return document
        if kind and kind.group(1).strip() == form and fallback is None:
            fallback = document
    if fallback is None:
        raise ValueError("Primary filing document not found in SEC archive")
    return fallback


def extract_filing(client: SECClient, filing: Filing) -> str:
    try:
        body = client.get_bytes(filing.source_url)
        if not filing.primary_document:
            body = _archive_document(body, "", filing.form)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        archive = f"{WWW}/Archives/edgar/data/{filing.cik}/{filing.accession}.txt"
        body = _archive_document(client.get_bytes(archive), filing.primary_document, filing.form)
    text = parse_document(body)
    if not text:
        raise ValueError(f"No readable text for {filing.accession}")
    return text


def extract_facts(client: SECClient, ticker: str, cik: int) -> Iterator[Fact]:
    ticker = ticker.upper()
    if ticker not in TICKERS or cik <= 0:
        raise ValueError("Invalid starter ticker or CIK")
    payload = client.get_json(f"{DATA}/api/xbrl/companyfacts/CIK{cik:010d}.json")
    for taxonomy, concepts in payload.get("facts", {}).items():
        for concept, metadata in concepts.items():
            for unit, entries in metadata.get("units", {}).items():
                for entry in entries:
                    if entry.get("form") not in FORMS:
                        continue
                    accession = entry.get("accn", "")
                    if not ACCESSION.fullmatch(accession):
                        continue
                    yield Fact(
                        ticker=ticker, accession=accession, form=entry["form"],
                        taxonomy=taxonomy, concept=concept,
                        label=metadata.get("label", concept), unit=unit,
                        value=entry["val"], start=entry.get("start"), end=entry["end"],
                        filed=entry["filed"], fiscal_year=entry.get("fy"),
                        fiscal_period=entry.get("fp"), frame=entry.get("frame"),
                    )


def main(argv: list[str] | None = None) -> int:
    # JSONL may contain characters outside Windows' active code page. Keep the
    # output UTF-8 when stdout is redirected to a file or pipe.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Extract SEC filings without persisting them")
    parser.add_argument("ticker", choices=sorted(TICKERS))
    parser.add_argument("--user-agent", required=True, help="Descriptive name and contact email")
    parser.add_argument("--limit", type=int, default=1, help="Number of newest filings to extract")
    parser.add_argument("--facts", action="store_true", help="Also emit matching XBRL company facts")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    client = SECClient(args.user_agent)
    filings = discover_filings(client, args.ticker)
    for filing in filings[:args.limit]:
        print(json.dumps({"type": "filing", 
                          "metadata": asdict(filing),
                          "text": extract_filing(client, filing)}, ensure_ascii=False))
    if args.facts and filings:
        for fact in extract_facts(client, args.ticker, filings[0].cik):
            print(json.dumps({"type": "fact", **asdict(fact)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
