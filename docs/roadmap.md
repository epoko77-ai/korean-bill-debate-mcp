# Roadmap

## Shipped

- `v0.6`: user-keyed local live research across bills, status, minutes, and expert review reports.
- `v0.7`: hosted Streamable HTTP MCP with personal encrypted connection URLs.
- `v0.8`: English questions over Korean official records with explicit source-language metadata.
- `v0.9`: no-account web research workspace using request-scoped Open Assembly and LLM keys.

## Before calling the platform stable

- Move long-running research from one serverless request to an isolated worker and durable job
  queue with progress events, cancellation, and retry diagnostics.
- Add a shared cache for public Assembly documents without creating cross-tenant credential or
  private-query state.
- Put a distributed rate limit and abuse controls at ingress; the in-process limiter is not global
  across serverless instances.
- Eliminate shared mutable live-service diagnostics across concurrent hosted MCP requests.
- Add deployed smoke tests for the workspace and hosted MCP without exposing live credentials.
- Report each inspected meeting, skipped document, upstream quota response, and bounded-search gap
  in a structured diagnostic panel.
- Add expiry and revocation for hosted MCP connection tokens without breaking existing clients.

## Next product layer

- Optional accounts, encrypted provider-key vaults, explicit key revocation, and research history.
- Team workspaces, saved investigation templates, shareable redacted reports, and citation export.
- Per-user cost ceilings, model selection within an allowlist, and provider usage estimates before
  a run.
- English workspace UI after the Korean workflow and security model are stable.
- Optional semantic index over public documents already fetched for a user's investigations.
