# Web research workspace

The `v0.9` workspace is a no-account, bring-your-own-key path for people who want a complete answer
without first mounting an MCP server in another AI product.

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
export KBD_REMOTE_TOKEN_SECRET="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
uv sync --extra dev
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
| `KBD_WORKSPACE_MAX_MINUTES_PER_REQUEST` | `1` | Maximum minutes PDFs parsed in one run |
| `KBD_WORKSPACE_MAX_EVIDENCE_CHARS` | `60000` | Maximum serialized evidence sent to the LLM |
| `KBD_WORKSPACE_TEMP_DIR` | `/tmp` | Parent directory for request-scoped caches |

Provider model defaults are operator-controlled so a public UI cannot be used to select an
unexpectedly expensive model. `store: false` is sent to the OpenAI Responses API. Provider-side
handling remains governed by the user's provider agreement and settings.

## Current alpha limits

- A run stays inside one HTTP request and may approach the deployment timeout on a cold PDF fetch.
- Request caches are deleted after each response, so repeated workspace questions do not yet reuse
  downloaded documents.
- Rate limiting is per process; production ingress must enforce a distributed limit.
- There is no account, history, saved key, revocation screen, background job, or billing layer.

These limits are intentional for the first security boundary: no stored credentials and no
cross-user private state. The worker-and-job architecture in the roadmap is required before calling
the platform stable.
