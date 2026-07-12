# Product specification

The implementation follows `korean-bill-debate-mcp-spec.md`, supplied as the project brief.
Its v0.1 contract is a local-first Python 3.12+ MCP server and CLI that retrieves Korean
National Assembly speeches using hybrid search, restores surrounding context, and preserves
citation-ready provenance.

The synthetic bundled demo is intentionally labeled and is not an official parliamentary
record. The default MCP requires the user's own Open Assembly API key, retrieves official data
at request time, and uses SQLite only as a private local evidence cache.

## v0.1 acceptance criteria

- Stable meeting and speech identifiers with source hashes.
- SQLite storage and FTS5 lexical retrieval with structured filters.
- Replaceable local embedding provider and RRF hybrid ranking.
- Speaker-aware transcript parsing with explicit failure reporting.
- Context, meeting, committee, and official-source fields in results.
- CLI commands and five stdio MCP tools.
- A key-validation setup flow, tests, linting, and Python 3.12/3.13 CI.
