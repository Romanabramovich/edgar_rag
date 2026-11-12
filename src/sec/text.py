"""Readable filing text from primary documents or complete archives."""

from html.parser import HTMLParser
import re
from urllib.error import HTTPError

from .client import SECClient
from .models import WWW, Filing


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
