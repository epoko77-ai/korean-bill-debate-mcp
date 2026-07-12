# Official data sources

The server uses only National Assembly official systems:

- `open.assembly.go.kr`: bill discovery, processing status, and meeting metadata APIs.
- `record.assembly.go.kr`: official minutes PDFs linked by Open Assembly metadata.

It does not use `data.go.kr` or a third-party Assembly dataset. Users obtain their own Open Assembly
API key and pass it as `ASSEMBLY_OPEN_API_KEY` or store it through `kbd setup`.

Every parsed speech retains the official minutes URL, source locator, retrieval hash, and parser
version. Bill results retain the official detail URL and the time at which live status was checked.

The private SQLite database is a cache of material the user has already requested. It is not shipped
as a public corpus and is safe to delete; the MCP will rebuild needed evidence from official sources.
