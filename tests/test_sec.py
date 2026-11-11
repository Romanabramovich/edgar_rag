import json
import unittest
from email.message import Message
from urllib.error import HTTPError

from src.sec import (
    SECClient, discover_filings, extract_facts, extract_filing, parse_document, resolve_cik,
)


def columns(*rows):
    return {key: [row.get(key, "") for row in rows] for key in (
        "accessionNumber", "form", "filingDate", "reportDate", "primaryDocument"
    )}


class Response:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        return self.data[:size]


class FakeSEC:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, request.get_header("User-agent"), timeout))
        result = self.routes[request.full_url]
        if isinstance(result, Exception):
            raise result
        return Response(result)


class SECExtractionTests(unittest.TestCase):
    def test_discover_older_history_amendments_and_duplicate(self):
        base = "https://data.sec.gov/submissions/"
        recent = columns(
            dict(accessionNumber="0000012345-25-000002", form="10-K/A", filingDate="2025-02-04", reportDate="2024-12-31", primaryDocument="amend.htm"),
            dict(accessionNumber="0000012345-25-000001", form="10-K", filingDate="2025-02-03", reportDate="2024-12-31", primaryDocument="annual.htm"),
            dict(accessionNumber="0000012345-25-000003", form="8-K", filingDate="2025-02-05", primaryDocument="other.htm"),
        )
        older = columns(
            dict(accessionNumber="0000012345-25-000001", form="10-K", filingDate="2025-02-03", reportDate="2024-12-31", primaryDocument="annual.htm"),
            dict(accessionNumber="0000012345-00-000001", form="10-Q", filingDate="2000-04-01", reportDate="2000-03-31", primaryDocument="quarter.txt"),
        )
        fake = FakeSEC({
            "https://www.sec.gov/files/company_tickers.json": {"0": {"ticker": "KO", "cik_str": 12345}},
            base + "CIK0000012345.json": {"filings": {"recent": recent, "files": [{"name": "old.json"}]}},
            base + "old.json": older,
        })
        client = SECClient("Personal Research contact@example.com", opener=fake)
        filings = discover_filings(client, "KO")
        self.assertEqual([f.form for f in filings], ["10-K/A", "10-K", "10-Q"])
        self.assertEqual(len(filings), 3)
        self.assertIn("/12345/000001234525000002/amend.htm", filings[0].source_url)
        self.assertEqual(resolve_cik(client, "ko"), 12345)
        self.assertTrue(all(call[1] == "Personal Research contact@example.com" for call in fake.calls))

    def test_html_preserves_table_cells_and_ignores_scripts(self):
        text = parse_document(b"<html><head><title>noise</title></head><body><h1>Results</h1><table><tr><td>Revenue</td><td>1,234</td></tr></table><script>bad()</script><p>Operating &amp; other</p></body></html>")
        self.assertIn("Revenue | 1,234", text)
        self.assertIn("Operating & other", text)
        self.assertNotIn("noise", text)
        self.assertNotIn("bad()", text)

    def test_missing_primary_uses_archive_document(self):
        row = dict(accessionNumber="0000012345-00-000001", form="10-Q", filingDate="2000-04-01", reportDate="2000-03-31", primaryDocument="quarter.htm")
        base = "https://www.sec.gov/Archives/edgar/data/12345/"
        missing = HTTPError(base + "000001234500000001/quarter.htm", 404, "missing", Message(), None)
        archive = b"<DOCUMENT><TYPE>EX-99\n<FILENAME>other.htm\n<TEXT>Wrong</TEXT></DOCUMENT><DOCUMENT><TYPE>10-Q\n<FILENAME>quarter.htm\n<TEXT><p>Quarterly disclosure</p></TEXT></DOCUMENT>"
        fake = FakeSEC({base + "000001234500000001/quarter.htm": missing,
                        base + "0000012345-00-000001.txt": archive})
        from src.sec import _filing
        filing = _filing("KO", 12345, row)
        assert filing is not None
        self.assertEqual(extract_filing(SECClient("Research contact@example.com", opener=fake), filing), "Quarterly disclosure")

    def test_blank_primary_uses_archive_document(self):
        from src.sec import _filing
        row = dict(accessionNumber="0000012345-00-000001", form="10-Q", filingDate="2000-04-01", reportDate="", primaryDocument="")
        archive_url = "https://www.sec.gov/Archives/edgar/data/12345/0000012345-00-000001.txt"
        archive = b"<DOCUMENT><TYPE>10-Q\n<TEXT><p>Old quarterly disclosure</p></TEXT></DOCUMENT>"
        filing = _filing("KO", 12345, row)
        assert filing is not None
        self.assertEqual(filing.source_url, archive_url)
        self.assertEqual(extract_filing(SECClient("Research contact@example.com", opener=FakeSEC({archive_url: archive})), filing), "Old quarterly disclosure")

    def test_company_facts_keep_units_periods_and_amendments(self):
        url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000012345.json"
        payload = {"facts": {"us-gaap": {"Revenues": {"label": "Revenue", "units": {
            "USD": [
                {"accn": "0000012345-25-000001", "form": "10-K", "val": 100, "start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-03", "fy": 2024, "fp": "FY", "frame": "CY2024"},
                {"accn": "0000012345-25-000002", "form": "10-K/A", "val": 101, "start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-04"},
                {"accn": "0000012345-25-000003", "form": "8-K", "val": 999, "end": "2024-12-31", "filed": "2025-02-05"},
            ]}}}}}
        facts = list(extract_facts(SECClient("Research contact@example.com", opener=FakeSEC({url: payload})), "KO", 12345))
        self.assertEqual(len(facts), 2)
        self.assertEqual((facts[0].unit, facts[0].start, facts[0].end, facts[0].fiscal_period), ("USD", "2024-01-01", "2024-12-31", "FY"))
        self.assertEqual(facts[1].form, "10-K/A")

    def test_rate_limit_and_retry_after(self):
        now = [0.0]
        sleeps = []
        attempts = [0]

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        def opener(request, timeout):
            attempts[0] += 1
            if attempts[0] == 1:
                headers = Message()
                headers["Retry-After"] = "3"
                raise HTTPError(request.full_url, 429, "slow", headers, None)
            return Response(b"ok")

        client = SECClient("Research contact@example.com", opener=opener, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(client.get_bytes("https://data.sec.gov/test"), b"ok")
        self.assertEqual(client.get_bytes("https://data.sec.gov/test"), b"ok")
        self.assertEqual(sleeps[0], 3.0)
        self.assertAlmostEqual(sleeps[1], 0.2)
        self.assertEqual(attempts[0], 3)

    def test_rejects_oversized_response_and_bad_rate(self):
        with self.assertRaises(ValueError):
            SECClient("Research contact@example.com", requests_per_second=11)
        client = SECClient("Research contact@example.com", max_bytes=3, opener=FakeSEC({"https://data.sec.gov/test": b"1234"}))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            client.get_bytes("https://data.sec.gov/test")


if __name__ == "__main__":
    unittest.main()
