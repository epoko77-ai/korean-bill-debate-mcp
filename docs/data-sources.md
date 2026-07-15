# Official data sources

The server uses only National Assembly official systems:

- `open.assembly.go.kr`: bill discovery, processing status, and meeting metadata APIs.
- `record.assembly.go.kr`: official minutes PDFs linked by Open Assembly metadata.
- `likms.assembly.go.kr`: official bill detail pages and committee expert review-report PDFs,
  discovered and downloaded only when a related bill is requested.

It does not use `data.go.kr` or a third-party Assembly dataset. Users obtain their own Open Assembly
API key and pass it as `ASSEMBLY_OPEN_API_KEY` or store it through `kbd setup`.

Every parsed speech retains the official minutes URL, source locator, retrieval hash, and parser
version. Bill results retain the official detail URL, live-check time, and any on-demand review
report with its official PDF URL and content hash.

The private SQLite database is a cache of material the user has already requested. It is not shipped
as a public corpus and is safe to delete; the MCP will rebuild needed evidence from official sources.

## Assembly-term scope

The durable research planner carries the official date boundaries for Assembly terms 1 through 22,
transcribed from the National Assembly Minutes service's
[historical-term table](https://record.assembly.go.kr/assembly/mnts/minutes/search.do). It preserves
the institutional gaps between terms instead of inventing continuous dates. A calendar interval
that falls only in one of those gaps is outside Assembly scope, not a `no_records` result. An
explicit term, term range, date, or date range is intersected with those boundaries. Without an
explicit scope or exact bill number, the configured current term—term 22—is the default.

This is a **planning boundary**, not a claim that every source family contains records for all 22
terms. The official systems expose different historical depths, and an official record may be
available on a detail page even when there is no term-wide full-text endpoint.

## Empirically observed source boundaries

The `v1.1.0` release probes found official source rows beginning at the following terms. A `+` means
“observed from this term in the current official interface,” not a guarantee that every meeting,
bill, document, or page is present thereafter.

| Official evidence family | Earliest empirically observed term | Retrieval note |
|---|---:|---|
| Plenary minutes metadata | 1+ | Retrieved from the Open Assembly plenary dataset; linked PDFs remain the official record. |
| Committee minutes metadata | 2+ | This source may also carry records classified as subcommittee meetings. |
| Bill discovery and processing status | 10+ | Bill candidates and live status are separate official datasets; status is fetched by exact bill number. |
| Dedicated subcommittee list | 16+ | A zero-row result here does not prove that no subcommittee discussion occurred; the committee source must also be checked. |
| Committee expert review reports | Dynamic per bill | Each relevant bill's official detail page is checked for currently published report links; there is no assumed term-wide inventory. |

These observations must not be restated as a complete historical full-text corpus. The service does
not bundle one, and it does not claim that every historical PDF has been built, deployed, parsed,
or operationally verified.

## Interpreting source availability

Durable results aggregate raw partition provenance by official dataset and Assembly term before
relevance filtering. `source_availability` uses three mutually exclusive states:

- `records_found`: every planned partition completed and the official dataset returned one or more
  raw source rows.
- `no_records`: every planned partition completed successfully and the official dataset returned
  exactly zero raw rows. Its English wording is deliberately scoped: **“No records found in this
  Open Assembly dataset.”**
- `incomplete`: at least one planned partition or expected row is still missing, so the service
  cannot conclude that records are absent.

A topic search that accepts no relevant candidates is different from a raw official dataset with
zero rows. Likewise, a transport error, timeout, or unfinished page walk must never be converted
into `no_records`. Review-report lookup is dynamic per selected bill and is reported separately,
not generalized into a term-wide zero. For the dedicated subcommittee dataset, a
`no_records` result carries an additional warning to inspect committee minutes because that dataset
can contain subcommittee proceedings.
