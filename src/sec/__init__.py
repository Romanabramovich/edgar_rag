"""SEC extraction API used by the local importer."""

from .client import SECClient
from .discovery import discover_filings, resolve_cik
from .facts import extract_facts
from .models import Fact, Filing, TICKERS
from .text import extract_filing, parse_document

__all__ = [
    "SECClient", "Fact", "Filing", "TICKERS", "discover_filings",
    "resolve_cik", "extract_facts", "extract_filing", "parse_document",
]
