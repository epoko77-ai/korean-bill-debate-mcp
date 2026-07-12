# Architecture

The default runtime is live-first and user-keyed.

```text
MCP client
  → KasmTools
  → LiveAssemblyServices
      → Open Assembly bill/status APIs
      → official bill detail + expert review-report PDF
      → meeting discovery APIs
      → official minutes PDF fetcher
      → transcript parser and relation builder
      → private SQLite evidence cache
  → evidence-linked JSON with official citations
```

`ASSEMBLY_OPEN_API_KEY` belongs to the user running the local MCP process. API URLs are redacted
before storage or output. API response cache entries expire after 15 minutes. Minutes PDFs are
content-addressed by official URL and retained locally because published minutes are immutable; their
SHA-256 and source locator remain attached to parsed speeches.

The local database is not a required prepared corpus. A fresh database is created automatically, and
each request hydrates it from live official candidates before lexical search and context expansion.
Optional semantic indexing may accelerate a cache that has grown over time, but it is not required for
correctness or initial use.

Issue research is staged to keep official traffic bounded:

1. Expand high-signal statute and policy terms.
2. Search official bill discovery and refresh status for top candidates.
3. Discover and ingest an official expert review report for the top related bills when available.
4. Derive committee and candidate months from bill metadata or explicit dates.
5. Retrieve committee/plenary metadata for those scopes.
6. Rank candidates and ingest at most the configured number of minutes.
7. Search reports and parsed speeches, then reconstruct ordered discussion threads.
8. Return official bill/report/minutes URLs, locators, and live-check time.

The bounded strategy is necessary because Open Assembly does not expose a universal full-text search
over every historical speech. The response reports its evidence rather than claiming exhaustive
coverage beyond the meetings it inspected.
