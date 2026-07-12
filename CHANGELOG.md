# Changelog

## [0.6.1] - 2026-07-12

### Fixed

- Replaced unavailable PyPI-based setup commands with a verified GitHub release install path.
- Registered local MCP clients against the pinned public `v0.6.1` source.
- Moved client connection and first-question instructions to the top of both READMEs.
- Removed unpublished registry metadata that pointed to a nonexistent PyPI package.

## [0.6.0] - 2026-07-12

### Added

- User-keyed live Open Assembly bill, status, meeting, and minutes research.
- Bounded lazy minutes ingestion into a private local SQLite cache.
- Secure setup command for Claude Code, Codex, Gemini CLI, and Claude Desktop.
- Live-check metadata and official citations in issue research results.
- On-demand discovery, PDF extraction, full-text search, and bill linking for official committee
  expert review reports.

### Removed

- Key-free prepared database and vector-index bootstrap.
- Rolling public data release and operator-keyed scheduled backfill.
- Prepared-corpus utility claims that did not measure fresh-cache live behavior.
