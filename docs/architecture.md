# Architecture

`v1.1.0` has three user-keyed surfaces with deliberately different execution boundaries.

```text
Hosted durable MCP
  → OAuth or legacy personal token → request-scoped Open Assembly key
  → start_research → exact leaf (32) + bulk leaf (24) + control (8)
  → isolated metadata/document workers
  → immutable job, coverage, overview, evidence-index, and source-text artifacts
  → status → complete map → core/selected text → optional exhaustive traversal

Local stdio MCP
  → user environment or private credentials file
  → live Open Assembly lookup + private SQLite cache
  → eight compatibility tools

Web workspace alpha
  → Open Assembly key + LLM key in one HTTPS request
  → synchronous live research → provider synthesis → source cards
```

## Durable scope, identity, and source accounting

The 13-tool hosted path separates interpretation, official-source collection, relevance, and final
evidence linkage. Its `v1.1.0` flow is:

```text
original natural-language question
  → immutable scope: term/date range + exact bill numbers + exact proposer roles
  → official term intersections and dataset partitions
  → raw per-dataset/per-term source availability
  → independent bill relevance and exact proposer-role gates
  → exact bill-number agenda linkage for meetings
  → status, overview, evidence index, and lossless source text
```

The term catalog covers official Assembly boundaries 1–22, including institutional gaps. Explicit
Korean term/range expressions and explicit calendar ranges select every intersecting term; no
scope defaults to the configured current term, currently 22. An exact seven-digit bill number binds
its own term and cannot be silently searched in a conflicting one.

Proposer identity is a hard filter rather than a relevance boost. Representative proposers are
checked against `RST_PROPOSER`, co-proposers against `PUBL_PROPOSER`, and a role-agnostic proposer
against their union, always with full Korean-name boundaries. A subject supplied with the name is
an independent hard gate. Meeting rows do not carry proposer identity, so they can enter a
proposer-scoped result only after a bill passes those checks and the meeting's official agenda
contains that bill's exact number.

`source_availability` is calculated from raw partition provenance before candidate filtering.
`records_found` and `no_records` require every planned partition and expected raw row to be present;
otherwise the state is `incomplete`. Consequently, a relevance-filtered empty result cannot be
misreported as an empty official dataset, and an API failure cannot become “No records found.” The
zero message is deliberately dataset-scoped. A dedicated subcommittee zero also warns that the
committee dataset can carry subcommittee proceedings.

The planner's historical range is broader than the historical depth of any one API family. Current
empirical probes begin at plenary 1+, committee 2+, bill/status 10+, and dedicated subcommittee 16+;
expert review reports are discovered dynamically per bill. See [Official data sources](data-sources.md).
None of these boundaries claims a complete historical full-text corpus.

The local compatibility runtime remains live-first and user-keyed.

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

Legacy/local issue research is staged to keep official traffic bounded:

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

## Hosted MCP and workspace boundaries

The hosted MCP and workspace do not share a credential or execution lifecycle.

```text
Hosted MCP: Open Assembly key → OAuth/token capability → queue + workers → durable MCP evidence

Workspace:  Open Assembly key ─→ request-scoped live research ─┐
            LLM key ─────────────────────── provider synthesis ├→ answer + official source cards
            question ──────────────────────────────────────────┘
```

Workspace research uses a new temporary directory for every HTTP request and removes it after
synthesis. The Assembly key never enters the LLM request, and the LLM key never enters the Assembly
client. The browser does not persist either key in cookies or web storage.

The durable queue, immutable run artifacts, and isolated document workers now exist for hosted MCP.
Exact-bill and interactive leaf tasks use the deployment-pinned `kbd-research` topic with a
32-message ceiling. Broad, non-exact leaves use `kbd-research-bulk` with a 24-message ceiling, while
broad coordinators and barriers remain on that same fully isolated bulk topic. Exact/interactive
fan-out coordinators and readiness/finalization barriers use `kbd-research-control` with an
8-message ceiling. The configured trigger ceilings total 64. They isolate Queue admission; they do
not claim a separate project-wide compute reservation.

Broad ingress publishes one durable dispatcher, which then opens fixed, bounded discovery shards on
the bulk lane; deferred routing follows the same fixed-shard model. One global completion barrier
per phase verifies every immutable readiness marker before the workflow advances. Broad document
hydration likewise makes bounded fixed shards runnable while finalization still requires every
compact completion receipt. Exact-bill work remains sequentially readiness-gated, so bulk traffic
cannot consume either exact queue budget. All three paths retain the
same at-least-once, idempotency, completion-receipt, retry, and same-deployment dispatch rules.
This internal split does not change the stable public `/mcp` endpoint or previously issued
`/mcp/t/...` capability URLs.
The workspace still uses the earlier single-request alpha boundary and does not inherit background
retries or durable progress. Distributed ingress limits, cancellation, legacy artifact migration,
and production corpus/deployment validation remain before platform stability. See [the workspace
design](workspace.md) and [roadmap](roadmap.md).
