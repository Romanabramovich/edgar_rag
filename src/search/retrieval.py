"""Keyword, semantic, and reciprocal-rank fused retrieval."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable

from .config import SearchConfig
from .indexer import Embedder, vector_search


def _filters(*, ticker: str | None, form: str | None,
             filed_from: str | None, filed_to: str | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if ticker:
        clauses.append("f.ticker=?")
        params.append(ticker.upper())
    if form:
        clauses.append("f.form=?")
        params.append(form.upper())
    if filed_from:
        clauses.append("f.filed>=?")
        params.append(filed_from)
    if filed_to:
        clauses.append("f.filed<=?")
        params.append(filed_to)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _fts_query(query: str) -> str:
    terms = re.findall(r"[\w]+(?:[.-][\w]+)*", query, flags=re.UNICODE)
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _metadata(connection: sqlite3.Connection, passage_id: int, *,
              score: float, rank: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT i.passage_id,i.accession,i.ordinal,p.body,f.ticker,f.form,f.filed,"
        "f.report_date,f.filing_url,f.source_url "
        "FROM passage_index AS i JOIN passages AS p "
        "ON p.accession=i.accession AND p.ordinal=i.ordinal "
        "JOIN filings AS f ON f.accession=i.accession WHERE i.passage_id=?",
        (passage_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Search result references missing passage {passage_id}")
    keys = ("passage_id", "accession", "ordinal", "body", "ticker", "form",
            "filed", "report_date", "filing_url", "source_url")
    result: dict[str, Any] = dict(zip(keys, row))
    result.update(score=score, rank=rank)
    return result


def keyword_search(connection: sqlite3.Connection, query: str, *,
                   limit: int = 50, ticker: str | None = None,
                   form: str | None = None, filed_from: str | None = None,
                   filed_to: str | None = None) -> list[dict]:
    if limit < 1:
        raise ValueError("limit must be positive")
    fts_query = _fts_query(query)
    if not fts_query:
        return []
    filter_sql, filter_params = _filters(
        ticker=ticker, form=form, filed_from=filed_from, filed_to=filed_to,
    )
    rows = connection.execute(
        "SELECT fts.rowid,bm25(passage_fts) AS score "
        "FROM passage_fts AS fts JOIN passage_index AS i ON i.passage_id=fts.rowid "
        "JOIN filings AS f ON f.accession=i.accession "
        "WHERE passage_fts MATCH ?" + filter_sql +
        " ORDER BY score ASC,i.passage_id ASC LIMIT ?",
        [fts_query, *filter_params, limit],
    ).fetchall()
    return [_metadata(connection, int(row[0]), score=float(row[1]), rank=rank)
            for rank, row in enumerate(rows, start=1)]


def semantic_search(connection: sqlite3.Connection, query: str, embedder: Embedder, *,
                    config: SearchConfig = SearchConfig(), limit: int | None = None,
                    ticker: str | None = None, form: str | None = None,
                    filed_from: str | None = None,
                    filed_to: str | None = None) -> list[dict]:
    rows = vector_search(
        connection, query, embedder, config=config, limit=limit,
        ticker=ticker, form=form, filed_from=filed_from, filed_to=filed_to,
    )
    return [_metadata(connection, row["passage_id"], score=row["distance"], rank=row["rank"])
            for row in rows]


def reciprocal_rank_fusion(keyword: Iterable[dict], semantic: Iterable[dict], *,
                           rrf_k: int = 10, limit: int = 10) -> list[dict]:
    if rrf_k < 1 or limit < 1:
        raise ValueError("rrf_k and limit must be positive")
    fused: dict[int, dict] = {}
    for branch, rank_field in ((keyword, "keyword_rank"), (semantic, "semantic_rank")):
        for rank, result in enumerate(branch, start=1):
            passage_id = int(result["passage_id"])
            item = fused.setdefault(passage_id, dict(result))
            item["rrf_score"] = item.get("rrf_score", 0.0) + 1.0 / (rrf_k + rank)
            item[rank_field] = rank
    ordered = sorted(
        fused.values(),
        key=lambda item: (-item["rrf_score"], item["ticker"],
                          -int(item["filed"].replace("-", "")),
                          item["accession"], item["ordinal"]),
    )
    for rank, item in enumerate(ordered, start=1):
        item["rank"] = rank
    return ordered[:limit]


def retrieve(connection: sqlite3.Connection, query: str, embedder: Embedder, *,
              config: SearchConfig = SearchConfig(), ticker: str | None = None,
              form: str | None = None, filed_from: str | None = None,
              filed_to: str | None = None) -> list[dict]:
    """Run filtered keyword/vector retrieval and fuse the ranked candidates."""
    if not query.strip():
        return []
    keyword = keyword_search(
        connection, query, limit=config.candidate_depth, ticker=ticker, form=form,
        filed_from=filed_from, filed_to=filed_to,
    )
    semantic = semantic_search(
        connection, query, embedder, config=config, limit=config.candidate_depth,
        ticker=ticker, form=form, filed_from=filed_from, filed_to=filed_to,
    )
    return reciprocal_rank_fusion(
        keyword, semantic, rrf_k=config.rrf_k, limit=config.result_limit,
    )


def search(connection: sqlite3.Connection, query: str, embedder: Embedder, *,
           config: SearchConfig = SearchConfig(), ticker: str | None = None,
           form: str | None = None, filed_from: str | None = None,
           filed_to: str | None = None) -> dict:
    """Backend-facing response shape with evidence and index coverage."""
    return {
        "query": query,
        "results": retrieve(
            connection, query, embedder, config=config, ticker=ticker, form=form,
            filed_from=filed_from, filed_to=filed_to,
        ),
        "coverage": search_coverage(connection, config=config),
    }


def search_coverage(connection: sqlite3.Connection, *,
                    config: SearchConfig = SearchConfig()) -> dict:
    total = connection.execute("SELECT COUNT(*) FROM passage_index").fetchone()[0]
    embedded = connection.execute(
        "SELECT COUNT(*) FROM passage_index WHERE embedding_model=? AND body_hash IS NOT NULL",
        (config.embedding_model,),
    ).fetchone()[0]
    date_range = connection.execute(
        "SELECT MIN(f.filed),MAX(f.filed) FROM passage_index AS i "
        "JOIN filings AS f ON f.accession=i.accession WHERE i.embedding_model=?",
        (config.embedding_model,),
    ).fetchone()
    return {"passage_count": total, "embedded_count": embedded,
            "pending_count": total - embedded,
            "embedding_model": config.embedding_model,
            "indexed_from": date_range[0], "indexed_to": date_range[1]}
