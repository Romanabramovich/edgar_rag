# Implement hybrid search in tested steps

## Summary

The local database already contains the planned corpus: 348 stored filings, 51,561 passages, and 103,993 facts. Search indexing and retrieval are the remaining work. Implement each step together with its unit tests, then run that step’s targeted tests before moving on.

## Step-by-step implementation and tests

1. **Add search storage and migration.** Add FTS5 indexing for passages and vector storage keyed to the same passage identifiers. Make database initialization handle existing databases safely.
   - **Unit tests:** opening a new database creates the search structures; opening the existing schema preserves filing data; passage rows can be indexed and mapped back to their accession and ordinal.

2. **Build the resumable indexer.** Index the existing 51,561 passages without refetching filings. Generate embeddings through Ollama/Nomic and persist them in `sqlite-vec`. Support resuming after interruption and index future imports through the same path.
   - **Unit tests:** mock embedding generation; verify successful indexing, reprocessing without duplicates, resume behavior, and clear handling of embedding or vector-storage failures.

3. **Implement independent retrieval branches.** Add FTS5 keyword retrieval and vector retrieval. Apply issuer, form, and filing-date filters to both; return passage text and citation metadata.
   - **Unit tests:** each branch returns expected results and honors each filter; empty queries and no-match cases behave safely; each passage resolves to its exact stored filing source.

4. **Add configurable RRF fusion.** Set `rrf_k = 10` and `candidate_multiplier = 5` initially. Compute each branch’s candidate depth as `rrf_k × candidate_multiplier` (50 initially). Make both settings configurable, and fuse the ranked lists while deduplicating shared passages.
   - **Unit tests:** verify rank-based ordering, overlap and single-branch results, deterministic ties, and recalculated candidate depth when either setting changes.

5. **Expose retrieval and tune context size.** Add a backend retrieval interface accepting a query and optional issuer, form, and date filters. Keep the final passage limit configurable and separate from candidate depth. Compare output sizes against reviewed questions and choose the smallest limit that retains the evidence needed to answer.
   - **Unit tests:** verify the interface applies filters, returns ranked citation-ready passages and coverage, and respects the configured final limit.

## Acceptance

- Run the targeted unit tests after each step, then the full unit suite.
- Evaluate multiple candidate depths and final passage limits using hand-reviewed narrative, numeric, and comparison questions. Record citation evidence coverage and latency.
- Confirm the indexer can process the existing corpus without duplicate rows and resume after interruption.

## Assumptions

- Keep the existing 1,800-character passages and 200-character overlap.
- Use the existing corpus; no additional historical backfill is part of this work.
