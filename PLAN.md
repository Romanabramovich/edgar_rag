## Summary

Build a free, self-hosted personal prototype with a React financial-terminal interface. It will answer cited questions about 10-K and 10-Q filings for **AAPL, NVDA, KO, and GOOG**, including narrative disclosures and reported figures. The first import will cover all historical data for KO and AAPL, and cover 10 years of data for NVDA and GOOG while being mindful of disk space permits. Priority goes to KO and AAPL, if only one ticker can have all historical data let it be KO.

## Implementation

- **Implementation Process**: Begin with the data extraction of the SEC filings, set up local storage, set up hybrid search functionality, set up LLM open model, any remaining misc backend work, implement front end only once all ETL, searching, and local model infra is confirmed. Stop at every step and wait for confirmation, expect pushback and re-iteration on features
- **Data ingestion:** A Python service will resolve tickers to SEC CIKs, fetch filing histories and XBRL company facts, and download 10-K/10-Q filings and amendments. It will retain parsed text, facts, source links, and import checkpoints while removing temporary downloads. Imports will be resumable, deduplicated, rate limited, and refreshed daily under the [SEC’s access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data). The [SEC APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) supply structured facts; filing archives supply narrative text.
- **Local storage and models:** Use SQLite for metadata and facts, FTS5 for keyword search, and `sqlite-vec` for semantic search, avoiding a Docker service, and following the Reciprocal Rank Fusion pattern for hybrid accuracy. Run the [2.5 GB Qwen3 4B Instruct model](https://ollama.com/library/qwen3%3A4b-instruct-2507-q4_K_M) and [Nomic embeddings](https://ollama.com/library/nomic-embed-text) through Ollama. Pin dependency versions. Start with filings from the latest five years, then backfill newest to oldest while preserving at least 6 GB free on C:. Show the actual indexed date range; full available history remains a resumable goal when capacity increases.
- **Answer pipeline:** Filter by issuer, form, and period; combine keyword and vector results; then supply a small set of source passages to the model. Resolve standardized figures from XBRL with their units and fiscal periods, and calculate comparisons in code. For older figures, report a value only when a filing passage directly supports it. Require source citations for factual claims and return an explicit insufficient-evidence answer when support is missing.
- **Interface and API:** Build a dark, responsive React chat with ticker and date filters, streamed answers, figure cards, a source-detail panel, direct SEC filing links, and visible index coverage and refresh status. A FastAPI backend will expose company/coverage data, source details, and a streaming chat endpoint. The SEC will be accessed by the backend because its data API does not support browser CORS.

## Verification

- Test resumable imports, amendments, duplicate handling, SEC throttling, and the disk reserve.
- Check fiscal-period and unit handling against known SEC facts; verify citations open the claimed filing and display the supporting passage.
- Evaluate a hand-reviewed set of narrative, numeric, comparison, missing-data, and conflicting-evidence questions. Reject invented citations and unsupported numbers.
- Check that the interface remains responsive during CPU inference and clearly distinguishes indexed history from available but unindexed filings.

## Assumptions

This is a single-user Windows 11 prototype running locally. The Intel Iris Xe machine is treated as CPU-first for inference. The 4 tickers above are the exact starter set; 10-K/10-Q and their amendments are the initial forms. The 6 GB free-space limit takes priority over completing the full historical backfill in the first release.
