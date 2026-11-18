PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    ticker TEXT PRIMARY KEY,
    cik INTEGER NOT NULL,
    discovered_at TEXT,
    facts_refreshed_at TEXT
);

CREATE TABLE IF NOT EXISTS filings (
    accession TEXT PRIMARY KEY,
    ticker TEXT NOT NULL REFERENCES companies(ticker),
    cik INTEGER NOT NULL,
    form TEXT NOT NULL,
    filed TEXT NOT NULL,
    report_date TEXT,
    primary_document TEXT NOT NULL,
    filing_url TEXT NOT NULL,
    source_url TEXT NOT NULL,
    stored_at TEXT
);

CREATE INDEX IF NOT EXISTS filings_ticker_filed ON filings(ticker, filed DESC);

CREATE TABLE IF NOT EXISTS filing_text (
    accession TEXT PRIMARY KEY REFERENCES filings(accession) ON DELETE CASCADE,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS passages (
    accession TEXT NOT NULL REFERENCES filings(accession) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY(accession, ordinal)
);

-- Stable IDs let the vector virtual table refer to a passage without depending
-- on SQLite's reusable implicit rowid. The FTS table is synchronized below.
CREATE TABLE IF NOT EXISTS passage_index (
    passage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    body_hash TEXT,
    embedding_model TEXT,
    embedded_at TEXT,
    UNIQUE(accession, ordinal),
    FOREIGN KEY(accession, ordinal) REFERENCES passages(accession, ordinal) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(
    accession UNINDEXED,
    ordinal UNINDEXED,
    body,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS passages_index_after_insert
AFTER INSERT ON passages BEGIN
    INSERT OR IGNORE INTO passage_index(accession, ordinal)
    VALUES (NEW.accession, NEW.ordinal);
END;

CREATE TRIGGER IF NOT EXISTS passage_index_fts_after_insert
AFTER INSERT ON passage_index BEGIN
    INSERT INTO passage_fts(rowid, accession, ordinal, body)
    SELECT NEW.passage_id, NEW.accession, NEW.ordinal, body
    FROM passages WHERE accession=NEW.accession AND ordinal=NEW.ordinal;
END;

CREATE TRIGGER IF NOT EXISTS passage_index_fts_after_delete
AFTER DELETE ON passage_index BEGIN
    DELETE FROM passage_fts WHERE rowid=OLD.passage_id;
END;

-- This also migrates passage rows from databases created before search indexing.
INSERT OR IGNORE INTO passage_index(accession, ordinal)
SELECT p.accession, p.ordinal
FROM passages AS p
WHERE NOT EXISTS (
    SELECT 1 FROM passage_index AS i
    WHERE i.accession=p.accession AND i.ordinal=p.ordinal
);

CREATE TABLE IF NOT EXISTS facts (
    fact_hash TEXT PRIMARY KEY,
    ticker TEXT NOT NULL REFERENCES companies(ticker),
    accession TEXT NOT NULL,
    form TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    label TEXT NOT NULL,
    unit TEXT NOT NULL,
    value_text TEXT NOT NULL,
    start TEXT,
    end TEXT NOT NULL,
    filed TEXT NOT NULL,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    frame TEXT
);

CREATE INDEX IF NOT EXISTS facts_lookup ON facts(ticker, taxonomy, concept, unit, end);
CREATE INDEX IF NOT EXISTS facts_accession ON facts(accession);

CREATE TABLE IF NOT EXISTS import_errors (
    accession TEXT PRIMARY KEY REFERENCES filings(accession) ON DELETE CASCADE,
    attempts INTEGER NOT NULL,
    last_error TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL
);
