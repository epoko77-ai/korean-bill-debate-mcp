# Korean Bill & Debate MCP

Current version: `v0.6.0`

**Explore the Korean National Assembly with a question. Verify with official records.**

From a bill's introduction and current status to committee review, expert analysis, and the actual
words exchanged by lawmakers and government officials, the server connects scattered Assembly
records into one evidence trail.

One question answers four parts of legislative research: where a bill stands, why it is moving or
stalled, who said what in context, and which official record proves it.

It is built for people working with legislation inside and outside the Assembly: parliamentary
staff, corporate policy and legal teams, public institutions and associations, researchers,
journalists, and civil-society organizations.

For deeper legislative analysis, it also surfaces two sources insiders look for first:
subcommittee negotiation records and committee expert review reports.

![Demo tracing a question from bills to actual remarks and surrounding context](assets/demo.gif)

Users provide their own Open Assembly API key. The server queries official APIs at request time,
downloads only relevant official minutes, parses the discussion, and keeps a private local cache for
repeat performance. It does not require or distribute a prebuilt Assembly database.

## Setup

Install `uv` and `pdftotext` (Poppler), then run one setup command:

```bash
uvx korean-bill-debate-mcp setup --client claude-code
uvx korean-bill-debate-mcp setup --client codex
uvx korean-bill-debate-mcp setup --client gemini
uvx korean-bill-debate-mcp setup --client claude-desktop
```

The wizard securely prompts for and validates your `ASSEMBLY_OPEN_API_KEY`. Manual configuration:

```json
{
  "mcpServers": {
    "korean-bill-debate": {
      "command": "uvx",
      "args": ["korean-bill-debate-mcp", "mcp"],
      "env": {"ASSEMBLY_OPEN_API_KEY": "YOUR_OPEN_ASSEMBLY_KEY"}
    }
  }
}
```

## Request flow

```text
natural-language question
  → live official bill and status lookup
  → relevant committee, plenary, or subcommittee discovery
  → bounded download and parsing of official minutes
  → bill–meeting–person–speech–reply connections
  → answer-ready evidence with official URLs and source locators
```

SQLite is a private local cache, not a bundled source database. Current bill status is refreshed from
the official status API. See the [Korean README](README.md) and [client guide](docs/mcp-clients.md).
