# Changelog

## [0.9.3] - 2026-07-13

### Fixed

- Allow the browser to return from the OAuth consent form to the exact registered Claude.ai or
  ChatGPT callback origin instead of blocking the redirect with the consent page CSP.
- Complete OAuth approval immediately instead of holding the browser open while the Open Assembly
  API performs a live validation request. Invalid credentials are reported on the first research
  request without delaying connector authorization.

## [0.9.2] - 2026-07-13

### Fixed

- Add MCP-standard OAuth 2.1 discovery, dynamic client registration, PKCE authorization, access
  tokens, and refresh tokens for Claude.ai and ChatGPT. Both now connect through the stable public
  `/mcp` URL instead of relying on a path capability that could register without exposing tools.
- Treat every seven-digit bill number in a natural-language question as an exact identifier and
  reject unrelated rows returned by fuzzy search or an upstream API response.
- Stop the web workspace before LLM synthesis when an explicit bill number cannot be verified,
  instead of substituting a different bill or review report.
- Forward the complete collected research evidence to the user's model without application-level
  character truncation or compaction.
- Increase answer budgets to 8,000 tokens by default and reject provider responses stopped at
  their token limit. Continue incomplete answers for up to three provider calls and combine every
  completed section, allowing long-form results without presenting a cut-off answer as complete.
- Keep backward-compatible personal MCP capability URLs in a stable path instead of a query
  parameter, while continuing to accept previously issued query-token URLs for legacy clients.
- Expand hosted research to as many as 20 relevant minutes PDFs and 50 matching speeches, include
  substantially longer expert-review excerpts, and scan every month from an explicitly requested
  bill's proposal through the present for chronological research.

## [0.9.1] - 2026-07-13

### Fixed

- Discover the Claude models available to each Anthropic API key and automatically choose an
  accessible Sonnet or Haiku model instead of assuming one fixed model ID.
- Distinguish Anthropic credit-balance, model-access, key-permission, rate-limit, and request-format
  errors without exposing raw provider responses or credentials.

## [0.9.0] - 2026-07-13

### Added

- A no-account Korean legislative research workspace at `/workspace` that connects one natural
  language question to live bills, status, expert review reports, subcommittee minutes, lawmakers'
  remarks, government answers, and verified official-source cards.
- Request-scoped OpenAI Responses API and Anthropic Messages API synthesis using each user's own
  LLM key, with provider-specific safe error handling and configurable model defaults.
- Session-only browser key handling without cookies or browser storage, per-request temporary
  research directories, JSON result export, strict response security headers, and a dedicated
  workspace request limit.

### Changed

- Corrected the security policy and roadmap to describe the already-shipped hosted user-key mode
  and the new two-key web workspace accurately.

## [0.8.0] - 2026-07-13

### Added

- English research requests with preserved original questions, Korean official-source search terms,
  and explicit `query_language`, `search_query_ko`, and `source_language` metadata.
- An optional `korean_query` argument for precise bilingual searches involving unfamiliar proper
  nouns, plus a built-in glossary for common legislative topics.
- MCP server instructions for English answers, faithfully translated quotations, Korean names, and
  claim-level official citations.
- Bilingual hosted connection and result pages, English prompts, and source-language guidance.

### Changed

- Updated the local installer source, demo badge, and client guides for `v0.8.0`.

## [0.7.1] - 2026-07-13

### Fixed

- Added a pure-Python PDF extraction fallback so hosted serverless MCP requests can parse official
  minutes and committee expert review reports without a system Poppler binary.
- Made `kbd setup` return a failure exit code when the selected client is missing or MCP
  registration fails instead of reporting command success.
- Validate each Open Assembly API key before issuing a personal hosted MCP link.
- Bound hosted requests to one relevant minutes PDF by default so a cold serverless request stays
  within the production execution window.

### Added

- Regression coverage for Poppler-free PDF extraction, failed client registration, and invalid
  hosted API keys.

## [0.7.0] - 2026-07-12

### Added

- Hosted Streamable HTTP MCP for ChatGPT and Claude web custom connectors.
- A no-install connection page that accepts each user's own Open Assembly API key and returns an
  encrypted personal MCP URL without storing the raw key.
- Request-scoped key isolation, missing-token rejection, connection rate limiting, and Vercel
  deployment configuration.

### Changed

- Web connection instructions now lead the Korean and English READMEs before local installation.

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
