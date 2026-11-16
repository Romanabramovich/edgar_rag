"""Extraction of standardized company facts with their reporting context."""

from typing import Iterator

from .client import SECClient
from .models import ACCESSION, DATA, FORMS, TICKERS, Fact


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
