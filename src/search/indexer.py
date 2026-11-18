"""Resumable passage embedding and sqlite-vec operations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
import sqlite3
import struct
from typing import Protocol, Sequence

from .config import SearchConfig


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorSearchError(RuntimeError):
    pass


def _load_vector_extension(connection: sqlite3.Connection, dimensions: int) -> None:
    try:
        import sqlite_vec
    except ImportError as exc:
        raise VectorSearchError(
            "sqlite-vec is missing; install dependencies from requirements.txt"
        ) from exc
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
    except (sqlite3.Error, OSError) as exc:
        raise VectorSearchError(f"Could not load sqlite-vec: {exc}") from exc
    finally:
        connection.enable_load_extension(False)

    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS passage_vectors "
        f"USING vec0(embedding float[{dimensions}])"
    )
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='passage_vectors'"
    ).fetchone()[0]
    if f"float[{dimensions}]" not in ddl:
        raise VectorSearchError(
            "Stored vector dimensions differ from SearchConfig; rebuild the vector index"
        )
    connection.executescript(
        "CREATE TRIGGER IF NOT EXISTS passage_index_vector_after_delete "
        "AFTER DELETE ON passage_index BEGIN "
        "DELETE FROM passage_vectors WHERE rowid=OLD.passage_id; END;"
    )


def _vector_blob(vector: Sequence[float], dimensions: int) -> bytes:
    if len(vector) != dimensions:
        raise VectorSearchError(
            f"Expected {dimensions} embedding values, received {len(vector)}"
        )
    values = [float(value) for value in vector]
    if any(not math.isfinite(value) for value in values):
        raise VectorSearchError("Embedding contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        raise VectorSearchError("Embedding cannot be a zero vector")
    return struct.pack(f"{dimensions}f", *(value / norm for value in values))


def _pending(connection: sqlite3.Connection, model: str,
             accessions: Sequence[str] | None, limit: int | None):
    clauses: list[str] = []
    params: list[object] = []
    if accessions is not None:
        if not accessions:
            return []
        placeholders = ",".join("?" for _ in accessions)
        clauses.append(f"i.accession IN ({placeholders})")
        params.extend(accessions)
    where = " WHERE " + " AND ".join(f"({clause})" for clause in clauses) if clauses else ""
    sql = (
        "SELECT i.passage_id,i.accession,i.ordinal,i.body_hash,i.embedding_model,p.body "
        "FROM passage_index AS i JOIN passages AS p "
        "ON p.accession=i.accession AND p.ordinal=i.ordinal "
        + where + " ORDER BY i.passage_id"
    )
    rows = connection.execute(sql, params).fetchall()
    pending = [row for row in rows
               if row[4] != model or row[3] != hashlib.sha256(row[5].encode("utf-8")).hexdigest()]
    if limit is not None:
        pending = pending[:limit]
    return pending


def index_pending(connection: sqlite3.Connection, embedder: Embedder, *,
                  config: SearchConfig = SearchConfig(), batch_size: int = 16,
                  limit: int | None = None,
                  accessions: Sequence[str] | None = None) -> int:
    """Embed missing/changed passages; committed batches make restarts safe."""
    if batch_size < 1 or (limit is not None and limit < 1):
        raise ValueError("batch_size and limit must be positive")
    _load_vector_extension(connection, config.embedding_dimensions)
    rows = _pending(connection, config.embedding_model, accessions, limit)
    embedded = 0
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        inputs = ["search_document: " + row[5] for row in batch]
        vectors = embedder.embed(inputs)
        if len(vectors) != len(batch):
            raise VectorSearchError("Embedder returned the wrong number of vectors")
        prepared = [(_vector_blob(vector, config.embedding_dimensions), row)
                    for vector, row in zip(vectors, batch)]
        with connection:
            for vector, row in prepared:
                body_hash = hashlib.sha256(row[5].encode("utf-8")).hexdigest()
                connection.execute(
                    "DELETE FROM passage_vectors WHERE rowid=?", (row[0],)
                )
                connection.execute(
                    "INSERT INTO passage_vectors(rowid,embedding) VALUES(?,?)",
                    (row[0], vector),
                )
                connection.execute(
                    "UPDATE passage_index SET body_hash=?,embedding_model=?,embedded_at=? "
                    "WHERE passage_id=?",
                    (body_hash, config.embedding_model,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"), row[0]),
                )
        embedded += len(batch)
    return embedded


def vector_search(connection: sqlite3.Connection, query: str, embedder: Embedder, *,
                  config: SearchConfig = SearchConfig(), limit: int | None = None,
                  ticker: str | None = None, form: str | None = None,
                  filed_from: str | None = None, filed_to: str | None = None) -> list[dict]:
    if not query.strip():
        return []
    candidate_limit = config.candidate_depth if limit is None else limit
    if candidate_limit < 1:
        raise ValueError("limit must be positive")
    _load_vector_extension(connection, config.embedding_dimensions)
    vectors = embedder.embed(["search_query: " + query])
    if len(vectors) != 1:
        raise VectorSearchError("Embedder returned the wrong number of query vectors")
    query_blob = _vector_blob(vectors[0], config.embedding_dimensions)

    filters = ["i.embedding_model=?", "i.body_hash IS NOT NULL"]
    params: list[object] = [config.embedding_model]
    if ticker:
        filters.append("f.ticker=?")
        params.append(ticker.upper())
    if form:
        filters.append("f.form=?")
        params.append(form.upper())
    if filed_from:
        filters.append("f.filed>=?")
        params.append(filed_from)
    if filed_to:
        filters.append("f.filed<=?")
        params.append(filed_to)
    candidate_ids = (
        "SELECT i.passage_id FROM passage_index AS i "
        "JOIN filings AS f ON f.accession=i.accession "
        "WHERE " + " AND ".join(filters)
    )
    rows = connection.execute(
        "SELECT v.rowid AS passage_id,v.distance FROM passage_vectors AS v "
        "WHERE v.embedding MATCH ? AND v.rowid IN (" + candidate_ids + ") "
        "ORDER BY v.distance LIMIT ?",
        [query_blob, *params, candidate_limit],
    ).fetchall()
    return [{"passage_id": row[0], "distance": float(row[1]), "rank": rank}
            for rank, row in enumerate(rows, start=1)]
