import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.search.config import SearchConfig
from src.search.embeddings import EmbeddingError, OllamaEmbedder
from src.search.indexer import VectorSearchError, index_pending
from src.search.retrieval import (
    keyword_search, reciprocal_rank_fusion, retrieve, search, search_coverage,
)
from src.importer import import_filings
from src.storage import open_database, save_catalog, save_filing
from src.sec import Filing


HAS_SQLITE_VEC = importlib.util.find_spec("sqlite_vec") is not None


class FakeEmbedder:
    model = "nomic-embed-text"

    def __init__(self):
        self.calls = []
        self.fail_on_call = None

    def embed(self, texts):
        self.calls.append(texts)
        if self.fail_on_call == len(self.calls):
            raise EmbeddingError("temporary Ollama failure")
        vectors = []
        for text in texts:
            vector = [0.0] * 768
            if "revenue" in text.lower() or "sales" in text.lower():
                vector[0] = 1.0
            else:
                vector[1] = 1.0
            vectors.append(vector)
        return vectors


def filing(ticker, accession, filed):
    return Filing(ticker, 12345 if ticker == "KO" else 67890, accession,
                  "10-K", filed, filed, "annual.htm", "https://sec.gov/filing/" + accession,
                  "https://sec.gov/document/" + accession)


@unittest.skipUnless(HAS_SQLITE_VEC, "install sqlite-vec from requirements.txt")
class HybridSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "edgar.sqlite"
        self.db = open_database(self.path, reserve_bytes=0)
        self.config = SearchConfig(result_limit=5)
        self.embedder = FakeEmbedder()
        ko = filing("KO", "0000012345-25-000001", "2025-02-03")
        aapl = filing("AAPL", "0000067890-25-000001", "2025-02-04")
        save_catalog(self.db, "KO", ko.cik, [ko])
        save_catalog(self.db, "AAPL", aapl.cik, [aapl])
        save_filing(self.db, ko, "Revenue increased as sales improved.",
                    reserve_bytes=0, disk_path=self.path.parent)
        save_filing(self.db, aapl, "Profit increased following new product launches.",
                    reserve_bytes=0, disk_path=self.path.parent)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_index_is_idempotent_and_resume_safe(self):
        self.assertEqual(index_pending(self.db, self.embedder, config=self.config, batch_size=1), 2)
        self.assertEqual(index_pending(self.db, self.embedder, config=self.config), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0], 2)

        another = filing("KO", "0000012345-25-000002", "2025-03-03")
        third = filing("KO", "0000012345-25-000003", "2025-04-03")
        save_catalog(self.db, "KO", another.cik, [
            filing("KO", "0000012345-25-000001", "2025-02-03"), another, third,
        ])
        save_filing(self.db, another, "Revenue and sales increased again.",
                    reserve_bytes=0, disk_path=self.path.parent)
        save_filing(self.db, third, "Revenue reached a new record.",
                    reserve_bytes=0, disk_path=self.path.parent)
        self.embedder.fail_on_call = len(self.embedder.calls) + 2
        with self.assertRaises(EmbeddingError):
            index_pending(self.db, self.embedder, config=self.config, batch_size=1)
        self.assertEqual(index_pending(self.db, self.embedder, config=self.config), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0], 4)

    def test_keyword_and_vector_retrieval_apply_filters(self):
        index_pending(self.db, self.embedder, config=self.config)
        keyword = keyword_search(self.db, "revenue sales", ticker="ko", limit=10)
        self.assertEqual(len(keyword), 1)
        self.assertEqual(keyword[0]["ticker"], "KO")
        self.assertEqual(keyword[0]["source_url"], "https://sec.gov/document/0000012345-25-000001")

        results = retrieve(self.db, "revenue", self.embedder, config=self.config,
                           ticker="KO", filed_from="2025-01-01", filed_to="2025-12-31")
        self.assertTrue(results)
        self.assertTrue(all(item["ticker"] == "KO" for item in results))
        self.assertIn("Revenue increased", results[0]["body"])

        no_match = retrieve(self.db, "revenue", self.embedder, config=self.config,
                            ticker="KO", filed_from="2026-01-01")
        self.assertEqual(no_match, [])

    def test_fusion_deduplicates_and_config_scales_candidate_depth(self):
        base = {"passage_id": 2, "ticker": "KO", "filed": "2025-01-01",
                "accession": "a", "ordinal": 0, "body": "x"}
        fused = reciprocal_rank_fusion(
            [dict(base, rank=1), dict(base, passage_id=3, rank=2)],
            [dict(base, rank=1), dict(base, passage_id=4, rank=2)],
            rrf_k=10, limit=3,
        )
        self.assertEqual([row["passage_id"] for row in fused], [2, 3, 4])
        self.assertEqual(fused[0]["rrf_score"], 2 / 11)
        self.assertEqual(SearchConfig().candidate_depth, 50)
        self.assertEqual(SearchConfig(rrf_k=20).candidate_depth, 100)
        self.assertEqual(SearchConfig(candidate_multiplier=8).candidate_depth, 80)
        with self.assertRaises(ValueError):
            SearchConfig(rrf_k=0)

    def test_search_coverage_reports_pending_and_date_range(self):
        self.assertEqual(search_coverage(self.db, config=self.config)["pending_count"], 2)
        index_pending(self.db, self.embedder, config=self.config)
        coverage = search_coverage(self.db, config=self.config)
        self.assertEqual((coverage["passage_count"], coverage["embedded_count"],
                          coverage["pending_count"]), (2, 2, 0))
        self.assertEqual(coverage["indexed_from"], "2025-02-03")

        response = search(self.db, "", self.embedder, config=self.config)
        self.assertEqual(response["query"], "")
        self.assertEqual(response["results"], [])
        self.assertEqual(response["coverage"]["embedded_count"], 2)

    def test_deleting_a_filing_removes_its_keyword_and_vector_rows(self):
        index_pending(self.db, self.embedder, config=self.config)
        ko = filing("KO", "0000012345-25-000001", "2025-02-03")
        with self.db:
            self.db.execute("DELETE FROM filings WHERE accession=?", (ko.accession,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM passage_index").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM passage_fts").fetchone()[0], 1)

    def test_import_embeds_new_passages_when_embedder_is_configured(self):
        item = filing("KO", "0000012345-25-000002", "2025-03-03")
        save_catalog(self.db, "KO", item.cik, [
            filing("KO", "0000012345-25-000001", "2025-02-03"), item,
        ])
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        catalogs = {"KO": [item], "AAPL": [], "NVDA": [], "GOOG": []}
        with patch("src.importer.refresh_catalog",
                   side_effect=lambda connection, client, ticker, **kwargs: catalogs[ticker]), \
             patch("src.importer.extract_filing", return_value="Revenue increased once more."):
            report = import_filings(
                self.db, object(), db_path=self.path, reserve_bytes=0, now=now,
                embedder=self.embedder, search_config=self.config, log=lambda *_: None,
            )
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.imported_accessions, [item.accession])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0], 1)


class EmbedderTests(unittest.TestCase):
    def test_ollama_client_parses_vectors_and_surfaces_bad_responses(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self):
                return b'{"embeddings":[[0.25,0.75]]}'

        seen = []

        def opener(request, timeout):
            seen.append((request.full_url, request.method, timeout, request.data))
            return Response()

        client = OllamaEmbedder(opener=opener)
        self.assertEqual(client.embed(["example"]), [[0.25, 0.75]])
        self.assertEqual(seen[0][:3], ("http://127.0.0.1:11434/api/embed", "POST", 120))

        with self.assertRaises(EmbeddingError):
            OllamaEmbedder(opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline"))).embed(["fail"])


if __name__ == "__main__":
    unittest.main()
