FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

LABEL io.modelcontextprotocol.server.name="io.github.epoko77-ai/korean-bill-debate-mcp"

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --extra deploy --no-dev

ENV KBD_DATA_DIR=/data

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "kasm.mcp.deployment:app", "--host", "0.0.0.0", "--port", "8000"]
