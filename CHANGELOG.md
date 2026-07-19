# Changelog

## [Unreleased]

### Added

- A release-published production OAuth smoke gate for the Claude.ai and ChatGPT-compatible
  13-tool surface, with bounded deployment-version waiting and no paid LLM credentials.

### Fixed

- Setup credential tests no longer read a developer's local `.env` or expose its value in a
  failing assertion.

## [1.1.1] - 2026-07-18

### Changed

- Route explicit top-N and “about five” questions to the bounded live Open Assembly path even when
  an MCP client accidentally selects `start_research`; reserve durable research for explicit
  exhaustive, multi-term, or structured unsupported scopes.
- Reduce the representative 2026 AI question from noisy sentence-level bill searches to three
  reviewed title queries and collapse a full calendar year of meeting metadata to three official
  API calls. Broad overviews no longer download every selected bill's review PDF; targeted
  `get_bill_status` still returns the lossless documents.
- Publish the observed legislative-progress, topical-relevance, and linked-discussion signals used
  for a bounded importance ranking, including its non-exhaustive caveat.

### Fixed

- Treat “2026년에 발의된” as a hard proposal-date filter. A 2025 bill can no longer pass because
  its committee registration or processing date falls in 2026, and stale cached bills or speeches
  outside the checked year are excluded from the bounded answer.
- Keep status polling small instead of reinserting the same 100-item overview on every poll.
- Expose an official bill URL and the available official deliberation URLs per bill, plus explicit
  source requirements for synthesis and a missing-discussion disclosure rule.

## [1.1.0] - 2026-07-18

### Added

- Add an immutable catalog of the National Assembly's official date boundaries for terms 1–22,
  including the institutional gaps between elected Assemblies. Natural Korean term expressions,
  term ranges, 19xx/20xx calendar ranges, and exact historical bill numbers now produce the exact
  intersecting term partitions instead of being restricted to terms 19–22.
- Add exact, role-aware proposer scope to research contracts, fingerprints, job persistence, and
  final relevance decisions. Representative, co-proposer, and role-agnostic requests are preserved
  separately and checked against `RST_PROPOSER`, `PUBL_PROPOSER`, or their union with full Korean
  name boundaries.
- Add per-dataset, per-term `source_availability` derived from raw immutable partition provenance.
  Terminal states distinguish `records_found`, successful `no_records`, and `incomplete` collection
  without using relevance-filtered candidate counts as a proxy for upstream availability.

### Changed

- Keep term 22 as the fast default only when a request contains no explicit term, date scope, or
  exact bill number. Explicit term lists remain non-contiguous when requested; ranges expand in
  chronological order; calendar ranges include every official term they actually intersect.
- Treat proposer identity and an accompanying legislative topic as independent hard gates. The
  representative-only path may use the official `PROPOSER` request parameter for acceleration, but
  accepted rows are still independently verified. Co-proposer and role-agnostic searches collect
  the required bill universe rather than relying on unsupported upstream filters.
- Document the empirically observed source depths separately from supported planning scope:
  plenary 1+, committee 2+, bill/status 10+, dedicated subcommittee 16+, and expert review reports
  discovered dynamically per relevant bill. Committee metadata can itself carry subcommittee
  proceedings.

### Fixed

- Link meetings in proposer-scoped research only after a bill passes the exact role/name gate and
  the official meeting agenda contains that bill's exact seven-digit number. Similar topic text can
  no longer attach an unrelated meeting or revive a rejected bill.
- Return a terminal empty result only when collection coverage is complete. A successful raw zero
  is described as “No records found in this Open Assembly dataset”; unfinished pages, API failures,
  and unproven coverage remain `incomplete`/inconclusive instead of being presented as historical
  absence.
- Add an explicit cross-source caveat when the dedicated subcommittee dataset returns zero, because
  committee minutes can still contain subcommittee discussion.
- Route receipt-less Queue callbacks through the public receive-by-ID API and upgrade the official
  Queue SDK to 0.4.0. Already-processed or expired deliveries close successfully, while temporary
  lease/ticket conflicts remain retryable instead of being acknowledged and stranded until their
  visibility timeout. Genuine authentication and Queue infrastructure failures still fail closed.
- Match the Queue lease to the bounded 270-second worker (300 seconds with SDK renewal) and reduce
  ambiguous delivery backoff from ten minutes to 30 seconds. Redeliveries check the durable
  task-completion receipt before repeating work; normal and terminal retries are now capped at 60
  seconds instead of creating multi-minute blind spots.
- Run broad, non-exact discovery, deferred metadata, and official-text hydration as fixed,
  bounded sixteen-item coordinator shards on an isolated bulk lane. One global completion barrier
  per phase verifies its complete immutable metadata plan before advancing, and finalization still
  requires every write-once terminal document outcome. The public gateway publishes one retryable dispatcher
  instead of synchronously opening every discovery shard. Exact-bill work keeps its sequential
  readiness-gated windows.
- Stop polling every full-text document outcome while hydration is incomplete. Receipt-gated chains
  carry their verified boundary into finalization instead of re-reading the full receipt set. Hosted
  run outcomes now keep a verified compact reference instead of duplicating the complete parsed PDF
  text per investigation; finalization restores those global parsed objects in bounded parallel reads.
  This preserves every parsed page segment and extracted character while removing multi-megabyte
  per-run PUTs and repeated full-text status polling.
- Resolve a warm official-document cache through immutable pointer metadata plus Blob HEAD size,
  without transferring the preserved PDF body before reading its parsed result. The private,
  content-addressed, no-overwrite Blob store is the warm-path integrity boundary; the source hash is
  established when raw bytes are first preserved. Concurrent cold-cache parsers now adopt the same
  canonical immutable winner even when their observation timestamps differ, while any difference in
  source identity, parser version, page text, or warnings still fails closed.
- Publish new immutable Vercel Blob result shards with one atomic put-if-absent request. Duplicate,
  conflicting, and ambiguous committed writes still read back and verify the exact bytes, while a
  first-pass broad snapshot no longer pays a preliminary GET for every index and text shard.
- Include a validated research ID and a bounded, credential-free last-progress snapshot in failed
  production-matrix results so a stalled live job can be traced without exposing its user API key.
- Partition the 64-message production Queue ceiling into 24 exact/interactive leaf slots on
  `kbd-research`, 32 fully isolated broad-work slots on `kbd-research-bulk`, and 8
  exact/interactive coordinator and barrier slots on `kbd-research-control`. Broad coordinators,
  barriers, metadata, and PDF work cannot consume either exact queue's admission budget. The
  recovery cron checks the matching consumer group for all three deployment-pinned topics. These
  are admission ceilings rather than reserved project compute; the production acceptance matrix
  exercises six exact and two complete broad investigations separately.
- Keep the public `/mcp` endpoint and previously issued `/mcp/t/...` capability URLs unchanged;
  the three-lane Queue split is internal and does not require Claude.ai or ChatGPT users to
  reconnect.
- Ship the shared Queue callback as an explicit deployable JavaScript module instead of leaving a
  TypeScript extension in Vercel's emitted runtime import. CI keeps that module byte-for-byte aligned
  with its strict TypeScript source, and the pre-deployment bundle smoke imports all three generated
  Queue handlers to catch missing shared modules before production traffic reaches them.
- Update the Claude.ai and ChatGPT web-connection guides to the current official plan availability,
  menu paths, OAuth approval flow, per-chat activation step, and public `/mcp` endpoint.
- Coalesce hosted broad first-page preview publication into the single retrying discovery barrier.
  Page workers no longer rescan the growing all-partition prefix independently, removing quadratic
  private-Blob reads while retaining the observed-only early map whenever follow-up pages remain.
  Raise bounded immutable-page read concurrency from 8 to 16 and check/assemble independent source
  partitions in batches of 8. This does not increase Open Assembly API request concurrency, alter
  deterministic ordering, or omit any source record.
- Avoid a second global read of every generic document-task receipt after broad fixed-window
  barriers have already verified their own ranges. Finalization still reads and validates every
  write-once terminal outcome; any missing document keeps the snapshot unavailable and retryable.

### Known limitations

- Planning support for terms 1–22 is not a claim that every official dataset starts at term 1 or
  that the public service contains a complete historical full-text corpus. Official source families
  have different empirical starting points, and historical PDFs have not all been built, deployed,
  parsed, and operationally verified as one corpus.
- Expert review-report availability is dynamic per bill detail page rather than a complete term-wide
  inventory. A missing or unfinished per-bill lookup must not be generalized to all bills in a term.
- The eight-tool local live-cache compatibility surface remains bounded and live-first. Durable
  multi-term coverage accounting and `source_availability` are provided by the fully configured
  13-tool hosted research surface.

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

- Publish a separately bound, observed-only first-page candidate map before broad discovery queue
  fan-out finishes. It is always labeled as incomplete (`metadata_inventory_complete=false`,
  unknown pending total, no substantive conclusion), while the original exhaustive discovery,
  source hydration, coverage accounting, and lossless evidence traversal continue unchanged.
- Bind complete-metadata pagination to an immutable `view_source_hash`, preventing a page walk from
  silently switching to the differently shaped final catalog when the snapshot becomes ready.

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
- Seed the seven-part exact-bill plan directly and reserve the eighth initial Queue message for its
  delayed barrier, removing a measured coordinator hop without exceeding the worker ceiling.
- Package the push callback and cron recovery bridge through one real JavaScript runtime module so
  Vercel recovery no longer imports a missing TypeScript function entry after compilation.

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
