# Deployment

## Local stdio (recommended)

Each user runs the MCP locally with their own Open Assembly API key. This keeps the credential and
query cache on the user's machine.

```bash
export ASSEMBLY_OPEN_API_KEY='YOUR_KEY'
uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.9.1 kbd mcp
```

## Hosted user-keyed Streamable HTTP

The public [connection page](https://korean-bill-debate-mcp.vercel.app) encrypts each user's
Open Assembly key into a personal connection token. The raw key is not stored in a database or file.

Self-hosters must set `KBD_REMOTE_TOKEN_SECRET` to a Fernet key. The setup page then issues personal
`/mcp?token=...` URLs. Never configure one shared Open Assembly operator key for public traffic.

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
