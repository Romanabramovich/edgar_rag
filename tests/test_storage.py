import tempfile
import unittest
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.importer import import_filings, planned_filings
from src.sec import Fact, Filing
from src.storage import (
    GIB, DiskReserveError, coverage, open_database, require_space,
    save_catalog, save_facts, save_filing, split_passages,
)


def filing(ticker, accession, filed, form="10-K"):
    return Filing(ticker, 12345, accession, form, filed, filed, "annual.htm",
                  "https://www.sec.gov/filing", "https://www.sec.gov/document")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "edgar.sqlite"
        self.db = open_database(self.db_path, reserve_bytes=0)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_atomic_filing_and_catalog_are_deduplicated(self):
        original = filing("KO", "0000012345-25-000001", "2025-02-03")
        amendment = filing("KO", "0000012345-25-000002", "2025-02-04", "10-K/A")
        save_catalog(self.db, "KO", 12345, [original, amendment])
        save_catalog(self.db, "KO", 12345, [original, amendment])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM filings").fetchone()[0], 2)
        self.assertTrue(save_filing(self.db, original, "Line one\nLine two", reserve_bytes=0, disk_path=self.db_path.parent))
        self.assertFalse(save_filing(self.db, original, "Changed", reserve_bytes=0, disk_path=self.db_path.parent))
        self.assertEqual(self.db.execute("SELECT body FROM filing_text").fetchone()[0], "Line one\nLine two")
        status = coverage(self.db)[0]
        self.assertEqual((status["available_count"], status["stored_count"]), (2, 1))
        self.assertEqual(status["stored_from"], "2025-02-03")

    def test_facts_keep_precision_units_and_deduplicate(self):
        save_catalog(self.db, "KO", 12345, [])
        fact = Fact("KO", "0000012345-25-000001", "10-K", "us-gaap", "Revenues",
                    "Revenue", "USD", 1234567890123456789, "2024-01-01", "2024-12-31",
                    "2025-02-03", 2024, "FY", "CY2024")
        self.assertEqual(save_facts(self.db, "KO", [fact], reserve_bytes=0, disk_path=self.db_path.parent), 1)
        self.assertEqual(save_facts(self.db, "KO", [fact], reserve_bytes=0, disk_path=self.db_path.parent), 0)
        row = self.db.execute("SELECT value_text,unit,start,end,fiscal_period FROM facts").fetchone()
        self.assertEqual(tuple(row), ("1234567890123456789", "USD", "2024-01-01", "2024-12-31", "FY"))
        corrected = Fact("KO", fact.accession, fact.form, fact.taxonomy, fact.concept,
                         fact.label, fact.unit, 1234567890123456790, fact.start, fact.end,
                         fact.filed, fact.fiscal_year, fact.fiscal_period, fact.frame)
        save_facts(self.db, "KO", [corrected], reserve_bytes=0, disk_path=self.db_path.parent)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT value_text FROM facts").fetchone()[0], "1234567890123456790")
        with self.assertRaisesRegex(ValueError, "Refusing"):
            save_facts(self.db, "KO", [], reserve_bytes=0, disk_path=self.db_path.parent)

    def test_refresh_removes_sec_deleted_filing(self):
        original = filing("KO", "0000012345-25-000001", "2025-02-03")
        replacement = filing("KO", "0000012345-25-000002", "2025-02-04")
        save_catalog(self.db, "KO", 12345, [original, replacement])
        save_filing(self.db, original, "Superseded text", reserve_bytes=0, disk_path=self.db_path.parent)
        save_catalog(self.db, "KO", 12345, [replacement])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM filings").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM filing_text").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM passage_index").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM passage_fts").fetchone()[0], 0)

    def test_passages_cover_exact_source_text(self):
        body = "First paragraph\n" + "x" * 300 + "\nLast paragraph"
        chunks = split_passages(body, max_chars=100, overlap_chars=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk in body for chunk in chunks))
        self.assertEqual(chunks[0][0], body[0])
        self.assertEqual(chunks[-1][-1], body[-1])

    def test_search_schema_indexes_passages_and_survives_reopen(self):
        item = filing("KO", "0000012345-25-000010", "2025-02-10")
        save_catalog(self.db, "KO", 12345, [item])
        save_filing(self.db, item, "Revenue improved meaningfully during the year.",
                    reserve_bytes=0, disk_path=self.db_path.parent)
        match = self.db.execute(
            "SELECT i.passage_id,i.accession,i.ordinal,p.body "
            "FROM passage_fts AS f JOIN passage_index AS i ON i.passage_id=f.rowid "
            "JOIN passages AS p ON p.accession=i.accession AND p.ordinal=i.ordinal "
            "WHERE passage_fts MATCH ?", ("meaningfully",),
        ).fetchone()
        self.assertIsNotNone(match)
        self.assertEqual((match["accession"], match["ordinal"]), (item.accession, 0))
        self.assertEqual(match["body"], "Revenue improved meaningfully during the year.")

        reopened = open_database(self.db_path, reserve_bytes=0)
        try:
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM filing_text").fetchone()[0], 1)
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM passage_index").fetchone()[0], 1)
            self.assertEqual(reopened.execute(
                "SELECT COUNT(*) FROM passage_fts WHERE passage_fts MATCH ?", ("meaningfully",)
            ).fetchone()[0], 1)
        finally:
            reopened.close()

    def test_open_database_migrates_legacy_passages_without_data_loss(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite"
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript("""
            CREATE TABLE companies(ticker TEXT PRIMARY KEY,cik INTEGER NOT NULL,
                discovered_at TEXT,facts_refreshed_at TEXT);
            CREATE TABLE filings(accession TEXT PRIMARY KEY,ticker TEXT NOT NULL,cik INTEGER NOT NULL,
                form TEXT NOT NULL,filed TEXT NOT NULL,report_date TEXT,primary_document TEXT NOT NULL,
                filing_url TEXT NOT NULL,source_url TEXT NOT NULL,stored_at TEXT);
            CREATE TABLE passages(accession TEXT NOT NULL,ordinal INTEGER NOT NULL,body TEXT NOT NULL,
                PRIMARY KEY(accession,ordinal));
            INSERT INTO companies VALUES('KO',12345,NULL,NULL);
            INSERT INTO filings VALUES('0000012345-25-000020','KO',12345,'10-K','2025-02-20',
                '2024-12-31','annual.htm','https://sec.gov/filing','https://sec.gov/doc','2025-02-20');
            INSERT INTO passages VALUES('0000012345-25-000020',0,'Legacy passage about revenue growth');
        """)
        legacy.close()

        migrated = open_database(legacy_path, reserve_bytes=0)
        try:
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM filings").fetchone()[0], 1)
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM passages").fetchone()[0], 1)
            self.assertEqual(migrated.execute(
                "SELECT COUNT(*) FROM passage_fts WHERE passage_fts MATCH ?", ("revenue",)
            ).fetchone()[0], 1)
        finally:
            migrated.close()

    def test_disk_reserve_blocks_write_before_transaction(self):
        fake_usage = lambda path: SimpleNamespace(free=6 * GIB + 10)
        with self.assertRaises(DiskReserveError):
            require_space(self.db_path.parent, 6 * GIB, disk_usage=fake_usage)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM filing_text").fetchone()[0], 0)

    def test_resumes_after_failed_filing_and_keeps_error_checkpoint(self):
        first = filing("KO", "0000012345-25-000002", "2025-02-04")
        second = filing("KO", "0000012345-25-000001", "2025-02-03")
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        save_catalog(self.db, "KO", 12345, [first, second], checked_at=now.isoformat(timespec="seconds"))
        for ticker in ("AAPL", "NVDA", "GOOG"):
            save_catalog(self.db, ticker, 12345, [], checked_at=now.isoformat(timespec="seconds"))
        self.db.execute("UPDATE companies SET facts_refreshed_at=?", (now.isoformat(timespec="seconds"),))
        self.db.commit()
        calls = []

        def once(client, item):
            calls.append(item.accession)
            if item.accession == first.accession and calls.count(first.accession) == 1:
                raise OSError("temporary outage")
            return f"Text for {item.accession}"

        with patch("src.importer.extract_filing", side_effect=once):
            report1 = import_filings(self.db, object(), db_path=self.db_path, reserve_bytes=0,
                                     now=now, log=lambda *_: None)
            report2 = import_filings(self.db, object(), db_path=self.db_path, reserve_bytes=0,
                                     now=now, log=lambda *_: None)
        self.assertEqual((report1.imported, report1.errors), (1, 1))
        self.assertEqual((report2.imported, report2.errors), (1, 0))
        self.assertEqual(calls, [first.accession, second.accession, first.accession])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM import_errors").fetchone()[0], 0)
        self.assertEqual(coverage(self.db)[0]["stored_count"], 2)

    def test_plan_prioritizes_recent_then_ko_and_aapl_history(self):
        catalogs = {
            "KO": [filing("KO", "1", "2025-01-01"), filing("KO", "2", "1994-01-01")],
            "AAPL": [filing("AAPL", "3", "2025-01-01"), filing("AAPL", "4", "1994-01-01")],
            "NVDA": [filing("NVDA", "5", "2025-01-01"), filing("NVDA", "6", "2018-01-01"), filing("NVDA", "7", "2010-01-01")],
            "GOOG": [filing("GOOG", "8", "2025-01-01"), filing("GOOG", "9", "2018-01-01")],
        }
        planned = planned_filings(catalogs, today=datetime(2026, 11, 11).date())
        self.assertEqual([f.accession for f in planned], ["1", "3", "5", "8", "2", "4", "6", "9"])


if __name__ == "__main__":
    unittest.main()
