# Korean Bill & Debate MCP

Current version: `v0.7.0`

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

![Demo tracing a question from bills to actual remarks and surrounding context](assets/demo.gif)

## Connect it to your AI in about three minutes

You do not need to learn a separate research interface. Connect the MCP once, then ask Claude,
Codex, or Gemini a normal question and let the tools retrieve the official Assembly evidence.

### Option 1: Use it on Claude.ai or ChatGPT web — no installation

Open the connection page first:

**https://korean-bill-debate-mcp.vercel.app**

1. Enter your personal Open Assembly API key.
2. Select **Create personal MCP link**.
3. Copy the complete `https://.../mcp?token=...` URL.
4. Paste that URL into your web app's custom MCP server field.

Claude: **Settings → Connectors → Add custom connector**, then enable it from the chat `+` menu.

ChatGPT: **Settings → Apps → Advanced settings → Developer mode → Create app**, then select the
created app from the chat `+` menu. Availability depends on your plan and workspace policy.

> A bare `/mcp` URL will not connect. Use the complete personal URL created from your own key. The
> service does not store the raw key in a database or file. Treat the generated URL like a password
> because anyone holding it can consume your Open Assembly API quota.

### Option 2: Install locally for Claude Desktop, Claude Code, Codex, or Gemini CLI

#### 1. Install the prerequisites

Issue your personal [Open Assembly API key](https://open.assembly.go.kr/portal/openapi/openApiNaListPage.do),
then install `uv` and Poppler (`pdftotext`).

```bash
# macOS
brew install uv poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### 2. Install the pinned GitHub release

```bash
uv tool install git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.7.0
```

#### 3. Run one command for the client you use

| AI client | One-time command | Connection |
|---|---|---|
| Claude Desktop | `kbd setup --client claude-desktop` | Local, automatic |
| Claude Code | `kbd setup --client claude-code` | Local, automatic |
| Codex | `kbd setup --client codex` | Local, automatic |
| Gemini CLI | `kbd setup --client gemini` | Local, automatic |

The setup wizard hides and validates your API key, stores it with user-only permissions, and
registers the MCP with the selected client. Your key and downloaded Assembly records stay on your
computer.

#### 4. Restart the client and ask

```text
For bill 2219564, connect its text and current status to relevant subcommittee minutes,
expert review reports, and statements by lawmakers and government officials. Cite official sources.
```

If the tool list includes `explore_issue`, `search_bills`, `get_bill_status`, and
`search_speeches`, setup is complete. See the [client-by-client guide](docs/mcp-clients.md) for UI
paths, manual configuration, verification, and troubleshooting.

Both web and local modes use each user's own Open Assembly API key. The hosted connection does not
store the raw key in a database or file. Local mode downloads only relevant official records and
keeps a private cache for repeat performance. Neither mode requires a prebuilt Assembly database.

## Request flow

```text
natural-language question
  → live official bill and status lookup
  → relevant committee, plenary, or subcommittee discovery
  → bounded download and parsing of official minutes
  → bill–meeting–person–speech–reply connections
  → answer-ready evidence with official URLs and source locators
```

In local mode, SQLite is a private cache rather than a bundled source database. Hosted instances use
ephemeral cache storage. Current bill status is refreshed from the official status API. See the
[Korean README](README.md) and [client guide](docs/mcp-clients.md).
