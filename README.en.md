# Korean Bill & Debate MCP

**Don't stop at the bill title. See who pushed it, who challenged it, and how the government
answered.**

One question connects current status, committees and subcommittees, lawmakers' actual words, and
the surrounding Q&A to official National Assembly sources.

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
