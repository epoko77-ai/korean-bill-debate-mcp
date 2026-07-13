# Web research workspace

The workspace is a no-account, bring-your-own-key alpha for people who want an answer without first
mounting an MCP server in another AI product. It remains a bounded single-request path; the durable
background research tools added in `v0.10.0` are currently an MCP-only surface.

## User flow

```text
Open Assembly key ─┐
                   ├─ live official research ─ official evidence ─┐
Question ──────────┘                                               ├─ answer + source cards
LLM provider key ─────────────────────────────── synthesis only ───┘
```

The Open Assembly key is never sent to the LLM provider. The LLM key is never passed into an
Assembly client. The selected provider receives the user's question and the official evidence that
must be summarized.

## Run locally

The workspace is part of the same ASGI deployment as the hosted MCP server. Generate a token secret
and start the deploy app:

```bash
uv sync --extra deploy
export KBD_REMOTE_TOKEN_SECRET="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
uv run uvicorn kasm.mcp.deployment:app --reload
```

Open `http://127.0.0.1:8000/workspace`. The keys entered in the page are sent only when **공식 기록
조사 시작** is pressed.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `KBD_OPENAI_MODEL` | `gpt-5.4-mini` | OpenAI Responses API model |
| `KBD_ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Preferred model; falls back to an accessible Sonnet/Haiku |
| `KBD_WORKSPACE_RATE_LIMIT_PER_MINUTE` | `6` | Per-process research request limit per IP |
| `KBD_WORKSPACE_MAX_MINUTES_PER_REQUEST` | `20` | Maximum relevant minutes PDFs parsed in one run |
| `KBD_OPENAI_MAX_OUTPUT_TOKENS` | `8000` | Requested output tokens per chunk; effective range `2200`–`16000` |
| `KBD_ANTHROPIC_MAX_OUTPUT_TOKENS` | `8000` | Requested output tokens per chunk; effective range `2200`–`16000` |
| `KBD_WORKSPACE_MAX_ANSWER_CHUNKS` | `3` | Automatic continuation chunks; effective range `1`–`5` |
| `KBD_WORKSPACE_TEMP_DIR` | `/tmp` | Parent directory for request-scoped caches |

Provider model defaults are operator-controlled so a public UI cannot be used to select an
unexpectedly expensive model. `store: false` is sent to the OpenAI Responses API. Provider-side
handling remains governed by the user's provider agreement and settings.

The workspace hard ceiling is therefore **16,000 requested output tokens per chunk × 5 chunks**.
The selected provider or model can enforce a lower input, context, or output limit. Official
evidence is sent to the provider without application-side slicing or compaction; if it exceeds the
model context window, the request fails with an explicit limit error instead of silently dropping
evidence. Likewise, if every continuation chunk reaches the output limit, the workspace returns an
explicit error and does not label the partial text as a complete answer.

Every successful JSON response includes `answer_delivery`. `status: "complete"` and
`partial: false` describe answer delivery only; research coverage remains separately reported in
`evidence.research_pagination`, `evidence.scope_inventory`, and the answer itself. The metadata also
reports the effective per-chunk request, maximum continuation chunks, the workspace hard limits,
and that provider/model limits still apply.

Source-card presentation is bounded to 180 title characters and 240 detail characters. The full
`title` and `detail` remain in the JSON response; only `presentation.title` and
`presentation.detail` are shortened. Each card reports the original/displayed character counts,
display limits, and `*_truncated` flags, and the browser visibly labels shortened cards. The JSON
download therefore preserves the complete source metadata alongside the official URL.

## Current alpha limits

- A run stays inside one HTTP request and may approach the deployment timeout on a cold PDF fetch.
- The provider/model context and output limits above can require a broad question to be split into
  multiple workspace requests; the workspace never presents a limit-stopped partial answer as
  complete.
- Request caches are deleted after each response, so repeated workspace questions do not yet reuse
  downloaded documents.
- Rate limiting is per process; production ingress must enforce a distributed limit.
- There is no account, history, saved key, revocation screen, workspace background job, or billing
  layer.

These limits are intentional for the first security boundary: no stored credentials and no
cross-user private state. The hosted MCP now has a worker-and-job architecture, but the workspace
must adopt and validate it separately before the platform can be called stable.
