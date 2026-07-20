# Roadmap

## Shipped

- `v0.6`: user-keyed local live research across bills, status, minutes, and expert review reports.
- `v0.7`: hosted Streamable HTTP MCP with personal encrypted connection URLs.
- `v0.8`: English questions over Korean official records with explicit source-language metadata.
- `v0.9`: no-account web research workspace using request-scoped Open Assembly and LLM keys.
- `v1.0`: durable hosted research jobs; exact coverage accounting; complete core-first maps;
  lossless source-text paging; and optional revision-bound corpus recall.

## Post-v1.0 hardening and hosted-service scale

The core MCP protocol and evidence contract are stable in `v1.0`. The separate no-account web
workspace remains an alpha product surface, and the public hosted service keeps the following
operational hardening work visible rather than overstating unproven corpus or scale guarantees.

- Build, publish, and independently audit a complete official-record corpus revision, then verify
  that the public deployment loads the pinned revision and never overstates broad-query recall.
- Add an explicit migration or versioned compatibility reader for research jobs created before the
  strict `v1.0` artifact schema.
- Keep the release-published 13-tool OAuth protocol and exact-research performance smoke green for
  Claude.ai and ChatGPT-compatible callbacks; refresh and review ChatGPT action snapshots when tool
  schemas change, and retain manual logged-in UI checks because automation does not impersonate
  either UI.
- Add cancellation and richer retry diagnostics to the durable research job surface.
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
