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
