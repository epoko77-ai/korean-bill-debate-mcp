# Contributing

Use Python 3.12 or 3.13 and `uv`. Keep official data credentials and raw minutes out of commits.

```bash
uv sync --extra dev --extra deploy --extra mcp
uv run ruff check .
uv run mypy
uv run pytest
npm ci
npm run check:queue
```

Parser changes must add or update a reviewed fixture, retain source locators, and report rather
than suppress unparsed regions. Retrieval changes must include qrels and publish the measured
artifact. Do not add `data.go.kr` or third-party parliamentary datasets: this project uses only
Open Assembly and official Assembly minutes.

Small focused pull requests are preferred. Explain the user-visible behavior, data provenance,
tests, and any index migration required.
