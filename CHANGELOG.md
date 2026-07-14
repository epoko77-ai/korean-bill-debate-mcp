# Changelog

## [1.0.0] - 2026-07-14

### Added

- Add a durable, queue-backed hosted research workflow with five MCP tools for starting one job,
  polling it, reading a complete core-first map, paging the evidence inventory, and opening exact
  official-text ranges. A fully configured hosted server exposes 13 tools; the local live-cache
  compatibility server continues to expose eight.
- Add immutable job, coverage, overview, evidence-index, source-text, and document artifacts bound
  to one research contract, index revision, and build. Publish the bounded overview before the
  snapshot readiness marker and load only the catalog shards needed for each page.
- Add exact bill-text evidence and deterministic bill, meeting, document, speech, question, answer,
  and government-response connections with official URLs, source hashes, and locators.
- Add an optional revision-bound corpus recall path and hosted Blob composition. Missing, stale, or
  scope-incomplete corpus coverage fails closed to an explicit partial result rather than proving
  completeness.

### Changed

- Separate the normal user flow into complete map first, prioritized core sources second, selected
  source text on demand, and explicit exhaustive traversal only when the user requests every
  record. Fast orientation no longer means silently dropping non-core entities.
- Route long evidence by exact ID, character count, SHA-256, URL, and locator instead of returning a
  shortened preview. Stable cursors reconstruct every stored range without application-level text
  loss.
- Preserve natural-language intent while using deterministic date, term, committee, exact bill
  number, and relevance accounting. Coverage and pagination must both be complete before the tools
  permit a comprehensive claim.
- Update the local installer, documentation, runtime version, lock file, and GitHub source pin to
  `v1.0.0`.

### Fixed

- Replace the hosted discovery and metadata checkpoints that duplicated every raw row and full
  rejected candidate with compact, readiness-gated boundaries. Immutable source pages retain the
  complete official payload, while accepted candidates, exact rejected identities and reasons,
  coverage accounting, resolver bindings, and deferred-work manifests remain restart-safe without
  repeatedly decoding a 50-70 MB run object.
- Persist generic write-once task-completion receipts after every worker's side effects. Queue
  redelivery can now distinguish a lost HTTP acknowledgement from unfinished work, skip duplicate
  coordinator fan-out, and avoid marking a late successful delivery as failed.
- Split Queue poison handling into ten normal attempts and marker-only later deliveries. Ambiguous
  network/timeout outcomes wait beyond the Python invocation limit before redelivery, exhausted
  messages never execute the expensive task again, and only a durable terminal marker permits an
  acknowledgement. Boundary-proven malformed messages and markers whose run has expired are
  acknowledged idempotently instead of being rescheduled until retention expires.
- Export the Queue consumer through the SDK's Connect-style Node callback required by a plain
  Vercel `api/*.ts` function. The previous Web-handler object built successfully but was never
  invoked by the production trigger, leaving accepted research jobs at zero completed pages.
- Write independent snapshot text, index, lookup, and overview shards with bounded concurrency,
  keep their manifests behind the completed shard set, and publish the summary as the final sole
  readiness marker. This removes the sequential Blob-write timeout while preserving idempotent
  crash recovery.
- Remove generic Korean instruction words from issue-term matching and cache deterministic query
  criteria during candidate resolution. Broad legislative questions no longer accept unrelated
  bills merely because they contain words such as “committee” or “official,” and the resolver no
  longer evaluates every rejected candidate twice.
- Build final bill groups and core bill bindings only from independently verified bill evidence.
  Bill numbers mentioned by a mixed meeting agenda remain available in immutable provenance and
  the evidence graph, but can no longer resurrect a resolver-rejected, unrelated bill in the
  answer-facing catalog.
- Keep exhaustive evidence traversal lossless while bounding each suggested follow-up page. The
  default next action now requests 20 inventory entries at a time, preserves the stable cursor
  until every entry has been visited, and opens long official text through exact hashed ranges
  instead of silently shortening it.
- Publish tiny page-readiness records only after their immutable raw source page, and check those
  fixed keys at incomplete discovery barriers instead of repeatedly downloading and decoding the
  full page bodies.
- Materialize accepted bills, status partitions, document work items, and bounded four-item route
  shards behind final readiness markers. Page, document, and finalization workers now read only
  their exact routing objects rather than decoding whole-run manifests once per task. Versioned
  compact state adopts in-flight legacy runs without write-once conflicts, while generation-bound
  600-second finalization claims prevent concurrent barriers from repeating full assembly and
  still permit crash recovery.
- Report the package release version in MCP `initialize.serverInfo.version` instead of leaking the
  installed MCP SDK version as the server version.
- Replace per-page and per-document whole-run completion scans with delayed, uniquely identified
  phase and finalization barriers. Concurrent workers can no longer all observe one another as
  incomplete and leave a research job permanently waiting after the last write.
- Bound hosted fan-out and Queue concurrency, chain broad partition/page/document publication, and
  run the worker in Seoul near the Open Assembly service. Excess work waits durably instead of
  creating an unbounded burst of Vercel Functions and Blob reads.
- Publish small write-once stage checkpoints for hosted status polling. New jobs require only three
  to five logical reads per poll and never scan partition pages, bill discoveries, or document
  outcomes merely to report progress; pre-checkpoint jobs retain the validated legacy fallback.
- Store the immutable research-job DAG in its own `job_state` artifact namespace, while reading and
  extending legacy `outcome` histories in place. Document inventories no longer amplify every job
  lookup, and orphan job events remain fail-closed.
- Read terminal document outcomes by their exact logical key and finalize the complete manifest in
  one barrier pass, eliminating quadratic outcome scans without dropping retry history or failed
  source coverage.
- Preserve legitimate duplicate-looking official agenda rows and reject only repeated complete
  pages, matching the Open Assembly client's source semantics without hiding pagination loops.
- Use the actual Vercel Blob 0.6 `list_objects` API and exhaust every cursor page for official
  document pointers and research artifact inventories, preventing hosted document jobs from
  failing SDK compatibility checks or silently omitting objects beyond the first list page.
- Treat a bodyless Vercel Queue HTTP 202 as a successful deferred publication, matching the
  official Queue SDK while retaining the task's idempotency identity for safe retries.
- Align wheel dependency metadata and official-source User-Agent versions with the v1.0 runtime,
  including direct cryptography and MCP 1.28 requirements used by the hosted OAuth path.
- Advertise every legislative research tool as read-only and non-destructive in MCP metadata, so
  ChatGPT plans and workspaces restricted to read/fetch actions can discover and enable them.
- Advertise and preserve `offline_access` through OAuth discovery, authorization, access, and
  refresh credentials so web connectors can retain access after the short-lived token expires.
- Read `ASSEMBLY_OPEN_API_KEY` during `kbd setup`, expose the documented `--api-key` fallback, and
  fail immediately without prompting in a non-interactive shell.
- Preserve bounded top-level Open Assembly error codes such as `ERROR-290` and `ERROR-300` instead
  of hiding them behind an unexpected-schema error, while continuing to redact credentials.
- Make matching existing Claude Code, Codex, and Gemini registrations idempotent while rejecting a
  conflicting command, and propagate a custom credentials path to the registered MCP process.
- Return a one-second bounded status-poll hint and raise the hosted MCP request budget so
  a normal long-form MCP investigation cannot be interrupted by its own progress checks.
- Match durable fan-out windows to the eight-worker Queue ceiling and remove the artificial delay
  between bounded windows, preserving complete traversal while eliminating serial four-item hops.
- Inline the bounded first candidate-map page in ready status responses, allowing web MCP clients
  to show useful progress without another serverless round trip while full source work continues.
- Keep interactive requests to four direct publications while eight-item durable coordinator
  windows use the full Queue ceiling; production measurements showed this lowers receipt latency.

### Known limitations

- The full official-record corpus revision has not yet been built, deployed, and operationally
  verified for the public service. Queries whose universe cannot be proven remain partial with
  explicit coverage gaps; `v1.0.0` does not claim a complete historical full-text index.
- Claude.ai and ChatGPT production-origin smoke tests cover dynamic registration, PKCE,
  `offline_access` refresh credentials, and the complete 13-tool read-only surface. Client plan
  entitlements, administrator policy, and approved-tool refresh behavior remain external.
- In-flight artifacts created before the strict `v1.0.0` schema do not yet have a migration path.
  Do not claim zero-downtime resumption of older research jobs.

## [0.9.3] - 2026-07-13 (untagged; included in 1.0.0)

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
