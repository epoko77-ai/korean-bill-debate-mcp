# Korean Bill & Debate MCP

Current version: `v0.6.1`

**Connect scattered Assembly records around a single bill.**

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

## Connect it to your AI in about three minutes

You do not need to learn a separate research interface. Connect the MCP once, then ask Claude,
Codex, or Gemini a normal question and let the tools retrieve the official Assembly evidence.

### 1. Install the prerequisites

Issue your personal [Open Assembly API key](https://open.assembly.go.kr/portal/openapi/openApiNaListPage.do),
then install `uv` and Poppler (`pdftotext`).

```bash
# macOS
brew install uv poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Install the pinned GitHub release

```bash
uv tool install git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.6.1
```

### 3. Run one command for the client you use

| AI client | One-time command | Connection |
|---|---|---|
| Claude Desktop | `kbd setup --client claude-desktop` | Local, automatic |
| Claude Code | `kbd setup --client claude-code` | Local, automatic |
| Codex | `kbd setup --client codex` | Local, automatic |
| Gemini CLI | `kbd setup --client gemini` | Local, automatic |
| ChatGPT web / Claude web | Requires a public HTTPS MCP URL | Not in the stock local release |

The setup wizard hides and validates your API key, stores it with user-only permissions, and
registers the MCP with the selected client. Your key and downloaded Assembly records stay on your
computer.

### 4. Restart the client and ask

```text
For bill 2219564, connect its text and current status to relevant subcommittee minutes,
expert review reports, and statements by lawmakers and government officials. Cite official sources.
```

If the tool list includes `explore_issue`, `search_bills`, `get_bill_status`, and
`search_speeches`, setup is complete. See the [client-by-client guide](docs/mcp-clients.md) for UI
paths, manual configuration, verification, and troubleshooting.

> ChatGPT and Claude web custom connectors connect to a remote server on the public internet; they
> cannot start this local `uvx` process. The stock release intentionally uses each person's API key
> and private local cache, so it does not advertise a shared public connector URL.

![Demo tracing a question from bills to actual remarks and surrounding context](assets/demo.gif)

Users provide their own Open Assembly API key. The server queries official APIs at request time,
downloads only relevant official minutes, parses the discussion, and keeps a private local cache for
repeat performance. It does not require or distribute a prebuilt Assembly database.

## Setup

Install `uv` and `pdftotext` (Poppler), then run one setup command:

```bash
kbd setup --client claude-code
kbd setup --client codex
kbd setup --client gemini
kbd setup --client claude-desktop
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
