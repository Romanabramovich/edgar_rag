"""Shared SEC record types and endpoint constants."""

from dataclasses import dataclass
import re


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
