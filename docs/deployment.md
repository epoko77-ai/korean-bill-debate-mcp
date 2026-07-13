# Deployment

## Local stdio (recommended)

Each user runs the MCP locally with their own Open Assembly API key. This keeps the credential and
query cache on the user's machine.

```bash
export ASSEMBLY_OPEN_API_KEY='YOUR_KEY'
uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.7.1 kbd mcp
```

## Hosted user-keyed Streamable HTTP

The public connection page is `https://korean-bill-debate-mcp.vercel.app`. It encrypts each user's
Open Assembly key into a personal connection token. The raw key is not stored in a database or file.

Self-hosters must set `KBD_REMOTE_TOKEN_SECRET` to a Fernet key. The setup page then issues personal
`/mcp?token=...` URLs. Never configure one shared Open Assembly operator key for public traffic.

## Private remote deployment (optional)

A remote deployment must not embed one shared operator key for arbitrary public traffic. Forward a
user-owned key through an authenticated secret channel, apply rate limits, and never persist the key.
The stock local setup is therefore the supported public distribution method.

```bash
ASSEMBLY_OPEN_API_KEY='SERVICE_ACCOUNT_FOR_PRIVATE_DEPLOYMENT' \
  kbd mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

Use this only for a private deployment whose operator accepts the upstream quota and credential risk.
Terminate TLS, restrict origins, redact request URLs, and protect the process with authentication.
