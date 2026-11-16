"""Small JSON Lines command for inspecting extracted SEC records."""

import argparse
from dataclasses import asdict
import io
import json
import sys

from . import SECClient, TICKERS, discover_filings, extract_facts, extract_filing


def main(argv: list[str] | None = None) -> int:
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
        print(json.dumps({"type": "filing", "metadata": asdict(filing),
                          "text": extract_filing(client, filing)}, ensure_ascii=False))
    if args.facts and filings:
        for fact in extract_facts(client, args.ticker, filings[0].cik):
            print(json.dumps({"type": "fact", **asdict(fact)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
