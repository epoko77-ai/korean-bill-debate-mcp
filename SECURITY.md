# Security policy

Do not open a public issue for a credential leak, code-execution issue, path traversal, unsafe
URL fetch, cross-tenant data exposure, or remote MCP denial-of-service vulnerability. Use GitHub's
private vulnerability reporting for this repository.

Supported security fixes target the latest release. Reports should include the affected version,
impact, reproduction, and suggested mitigation. Never include a live Open Assembly, OpenAI, or
Anthropic key in a report.

## Credential boundaries

This project has three deliberately separate operating modes.

### Local MCP

The user's `ASSEMBLY_OPEN_API_KEY` stays in the local MCP process. The local SQLite and document
cache are private to that user. No LLM key is needed because Claude, ChatGPT, Codex, or another MCP
host performs the language-model work.

### Hosted MCP connection

Claude.ai and ChatGPT use standard OAuth discovery, dynamic client registration, PKCE
authorization, and short-lived bearer access tokens. The HTTPS approval page receives the user's Open Assembly key
solely to check its bounded shape and place it inside an encrypted access credential. Approval does
not wait on the official API: the first Assembly-backed tool request validates the credential while
using it against the official service. The raw key is not written to a database or file. The hosted
process decrypts the credential for one authenticated MCP request and uses the key only to query
official Assembly services. Refresh credentials are also encrypted bearer credentials and must be
protected by both the client and operator.

For clients that cannot complete this OAuth flow, the connection page can issue a legacy
Fernet-encrypted path capability after validating the same user key.

The generated `/mcp/t/...` URL is a password-equivalent bearer credential. Anyone who has the
complete URL can consume that user's Open Assembly quota. Users must not publish, screenshot, or
send it through untrusted channels. Operators must avoid logging full query strings and rotate
`KBD_REMOTE_TOKEN_SECRET` if connection URLs are exposed.

### Web research workspace

The `/workspace` page sends the Open Assembly key, selected LLM provider, LLM key, and question in
one HTTPS JSON request. The server:

1. uses the Open Assembly key only for live official-source research;
2. creates a per-request temporary cache and deletes it after the response;
3. sends the question and researched official excerpts to the selected LLM provider;
4. uses the LLM key only in that provider request; and
5. returns the answer and an allowlisted set of `https://*.assembly.go.kr` source links.

The application does not put either key in a URL, response, cookie, database, file, browser
`localStorage`, or browser `sessionStorage`. Password fields remain only in the current page memory
so the user can ask again without retyping; reloading or closing the tab clears them. The chosen LLM
provider's own data-use and retention policy still applies to the question and official excerpts.

## Deployment requirements

- Terminate TLS before the application and never serve credential forms over plain HTTP.
- Disable request-body logging and redact `/mcp` query strings at the ingress and observability
  layers.
- Keep `KBD_REMOTE_TOKEN_SECRET` in the deployment secret store, not source control.
- Apply an authoritative distributed rate limit at ingress. The included limiter is per process and
  is only a last-line guard for small deployments.
- Permit document downloads only from verified official Assembly hosts.
- Treat official documents as untrusted prompt content. They may be quoted as evidence but must not
  override system or developer instructions.
- Review upstream provider policies and incident-response procedures before enabling the workspace
  for sensitive or regulated workloads.
