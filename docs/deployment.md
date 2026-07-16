# Deployment

## Local stdio (recommended)

Each user runs the MCP locally with their own Open Assembly API key. This keeps the credential and
query cache on the user's machine.

```bash
export ASSEMBLY_OPEN_API_KEY='YOUR_KEY'
uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v1.1.0 kbd mcp
```

## Hosted user-keyed Streamable HTTP

Claude.ai and ChatGPT should register the stable public endpoint
`https://korean-bill-debate-mcp.vercel.app/mcp`. It returns the MCP OAuth discovery challenge,
supports dynamic client registration and PKCE, and turns the Open Assembly key entered on the
approval page into expiring access and refresh credentials. The raw key is not stored in a database
or file.

The public [connection page](https://korean-bill-debate-mcp.vercel.app) also keeps a legacy
`/connect` form for clients that cannot complete OAuth. That form validates the user's key before
issuing a password-equivalent `/mcp/t/...` URL. Do not use the personal URL in Claude.ai or ChatGPT.

Self-hosters must set `KBD_REMOTE_TOKEN_SECRET` to a Fernet key. The setup page then issues personal
`/mcp/t/...` URLs. Never configure one shared Open Assembly operator key for public traffic.

The five durable research tools require the complete hosted research configuration: credential
secret, Blob storage, deployment identity, queue dispatch, and internal dispatch secret. A partial
configuration deliberately exposes only the eight live-cache compatibility tools and no worker.

### Durable production checklist

Before deploying the 13-tool surface:

1. Connect a **private** Vercel Blob store. Its project connection must expose
   `BLOB_READ_WRITE_TOKEN` to Production. Do not point the artifact adapters at a public store.
2. Keep the existing Fernet `KBD_REMOTE_TOKEN_SECRET`. Optionally set a separate Fernet
   `KBD_RESEARCH_CREDENTIAL_SECRET`; when omitted, the remote-token secret is reused.
3. Set `KBD_INTERNAL_TASK_SECRET` to an independent 32–512 byte printable ASCII secret. It protects
   the queue bridge's same-deployment Python dispatch boundary and must not be a user API key.
4. Set `CRON_SECRET` to another independent 32–512 byte printable ASCII value. The once-per-minute
   recovery route uses Vercel Queues poll mode to lease work still available in the primary push
   consumer group. Vercel Pro or Enterprise is required for this cadence; Hobby projects cannot
   deploy a once-per-minute cron expression.
5. Enable Vercel **Secure Backend Access with OIDC** and automatic System Environment Variables.
   Runtime requests need the `x-vercel-oidc-token` identity and the application requires the
   system-provided `VERCEL_DEPLOYMENT_ID` to keep queue messages deployment-bound. The queue bridge
   uses the system-provided `VERCEL_URL` for its same-deployment internal dispatch target; it never
   trusts an inbound Host header for that secret-bearing request.
6. Keep the queue topic at `kbd-research`, or change `KBD_RESEARCH_QUEUE_TOPIC` and the
   `experimentalTriggers[].topic` value in `vercel.json` together.
7. Allow the production host and the web-client origins in `KASM_ALLOWED_HOSTS` and
   `KASM_ALLOWED_ORIGINS`. The production smoke must cover both `https://claude.ai` and
   `https://chatgpt.com`.

Push delivery remains the primary path. Poll recovery uses that route's exact auto-derived consumer
group, `api_Squeues_Skbd-research_Dts`, rather than creating a second group that would receive another
copy of every task. It is deployment-pinned, leases at most four available messages concurrently,
and immediately sends each through the same private dispatcher with a mandatory completion-receipt
check. It processes at most 16 independently bounded messages per cron invocation and never
collapses a research job into one synchronous operation. Push and recovery races therefore converge
through the same immutable artifacts, queue idempotency keys, and write-once task receipts without
adding a receipt lookup to the normal first push delivery.

Both Node entry points import `serverless/kbd-research-shared.mjs`. Keep that deployable JavaScript
module and its `kbd-research-shared.d.mts` type contract together outside `api/`: importing one
generated TypeScript function entry from the other leaves no sibling module in Vercel's isolated
bundle, while placing the shared module under `api/` incorrectly creates a fifth public function.

The hosted defaults publish at most seven page tasks directly from the request; together with the
delayed phase barrier, an exact investigation seeds no more than eight initial Queue messages. This
removes a coordinator round trip for the complete seven-part exact-bill plan while preserving the
Queue trigger's concurrency ceiling. Larger plans seed independent durable sixteen-item coordinator
shards up front; no shard depends on a preceding coordinator to publish it, and each coordinator
still opens only a bounded worker window. The production consumer admits at most 32 in-flight
messages, leaving room for another user's exact search while a broad shard is active. Override
`KBD_RESEARCH_DIRECT_FANOUT_LIMIT`, `KBD_RESEARCH_FANOUT_CHUNK_SIZE`, and
`KBD_RESEARCH_FANOUT_DELAY_SECONDS` only together with a measured Queue concurrency change.

Do not set `KBD_RESEARCH_CORPUS_REVISION` merely to make the health field true. Set it only to a
published, complete revision whose readiness marker and referenced objects have been verified. It
is valid to leave it unset: the server then reports unproven broad scope as partial instead of
claiming complete historical recall.

### Historical-scope production checks

The `v1.1.0` planner knows the official date bounds for terms 1–22. That catalog expands an explicit
term/date range into deterministic source partitions; it is not a substitute for source readiness.
No scope defaults to the configured current term, currently term 22. Before describing historical
support in a deployment, verify all of the following:

1. The immutable contract and fingerprint retain every requested Assembly term, exact bill number,
   and representative/co/role-agnostic proposer name.
2. Representative-only discovery may use the upstream `PROPOSER` acceleration, but every accepted
   row is still checked against the official role field. Co-proposer and role-agnostic discovery
   must not rely on unsupported upstream filters.
3. Proposer-scoped meetings are admitted only through an exact seven-digit bill number on the
   official agenda. Topic similarity alone must not create a bill–meeting link.
4. Final overview artifacts retain `source_availability` per dataset and term. Only complete,
   successful zero-row partitions may emit `no_records`; unfinished or failed work must remain
   `incomplete`.
5. A dedicated subcommittee zero includes its cross-source caveat, and review-report availability
   is checked dynamically for each relevant bill.

The current empirical source-depth matrix—plenary 1+, committee 2+, bill/status 10+, dedicated
subcommittee 16+, and per-bill dynamic review reports—is documented in
[Official data sources](data-sources.md). It must not be converted into a claim that the deployment
contains a complete historical full-text corpus.

After deployment, `/healthz` must report `durable_research: true` and `mcp_tool_count: 13`. Then run
`scripts/smoke_remote_durable_oauth.py` once with `KBD_SMOKE_ORIGIN=https://claude.ai` and once with
`KBD_SMOKE_ORIGIN=https://chatgpt.com`. The smoke uses the official MCP SDK, dynamic registration,
PKCE, refresh credentials, read-only tool annotations, and a fast durable research receipt without
printing the Open Assembly key or OAuth tokens. An 8-tool health result is a failed durable rollout,
not a successful compatibility substitute.

The same deployment exposes the no-account research workspace at `/workspace`. It accepts a user's
Open Assembly and LLM keys in one HTTPS request, isolates them by purpose, and deletes its temporary
cache after the response. See [workspace configuration and alpha limits](workspace.md) and the
[security policy](../SECURITY.md) before enabling it publicly.

## Private operator-key deployment (optional)

A remote deployment must not embed one shared operator key for arbitrary public traffic. Forward a
user-owned key through an authenticated secret channel, apply rate limits, and never persist the key.
The stock public deployment therefore uses the hosted user-key connection and workspace modes above.

```bash
ASSEMBLY_OPEN_API_KEY='SERVICE_ACCOUNT_FOR_PRIVATE_DEPLOYMENT' \
  kbd mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

Use this only for a private deployment whose operator accepts the upstream quota and credential risk.
Terminate TLS, restrict origins, redact request URLs, and protect the process with authentication.
