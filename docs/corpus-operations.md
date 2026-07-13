# Revisioned corpus operations

`kbd-corpus` builds the exhaustive full-text recall layer used by durable
research. It inventories an Assembly term independently of any topic query,
then downloads every identified original bill, specialist review report, and
distinct minutes PDF. There is no top-N option.

The Open Assembly key is accepted only through `ASSEMBLY_OPEN_API_KEY` (or the
existing local credential store). Do not put it on a command line. Checkpoints,
inventory manifests, logs, and activation files never contain that key.

## 1. Inventory without downloading PDFs

```bash
export ASSEMBLY_OPEN_API_KEY='issued-by-open-assembly'

uv run kbd-corpus inventory \
  --term 22 \
  --checkpoint .state/corpus-22.json \
  --manifest-output .state/corpus-22-inventory.json
```

This completely paginates the term's bill and meeting datasets and checks the
exact document index of every bill. Bill-index progress is saved one bill at a
time. If the process is interrupted, run the same command again: the pinned
inventory observation time and successful index checks are resumed. A later,
finished inventory invocation starts a new observation and rechecks indexes.

Use `--dry-run` with `--manifest-output` to produce only the credential-free
inventory manifest without creating or changing a build checkpoint. Exit code
`3` means the manifest is honestly incomplete; its coverage matrix contains the
expected, discovered, failed, and unaccounted state.

## 2. Download, parse, and checkpoint every document

```bash
uv run kbd-corpus build \
  --checkpoint .state/corpus-22.json \
  --storage filesystem \
  --corpus-dir .state/corpus-objects \
  --document-cache-dir .state/official-documents
```

Each raw PDF is preserved before parsing. Each parsed document is written to
the content-addressed corpus store before its successful checkpoint is saved.
Transient downloads are retried three times by default; a rerun skips every
success and resumes retryable work. Permanent failures remain visible and
block publication unless explicitly rechecked with `--retry-permanent`.

Inspect progress at any time:

```bash
uv run kbd-corpus status --checkpoint .state/corpus-22.json
```

The status output reports `expected_count`, `succeeded_count`, `failed_count`,
and `unaccounted_count` for every Assembly-term/evidence-kind cell.

## 3. Publish and prepare activation

```bash
uv run kbd-corpus publish \
  --checkpoint .state/corpus-22.json \
  --storage filesystem \
  --corpus-dir .state/corpus-objects \
  --revision-manifest-output .state/corpus-22-revision.json \
  --activation-output .state/corpus-22-activation.json
```

Publication fails closed. An unknown expected count, one inventory gap, one
pending document, one parse/download failure, or an incomplete parent prevents
the repository revision, revision export, activation file, and revision ID
from being emitted. The activation file is created only after the immutable
revision passes its complete three-axis coverage and lexical-index binding.
It names the `KBD_RESEARCH_CORPUS_REVISION` value for the deployment operator;
it does not mutate a deployment by itself.

For a private Vercel Blob corpus, configure `BLOB_READ_WRITE_TOKEN` outside the
shell history and replace filesystem arguments with:

```bash
--storage vercel-blob --blob-prefix kbd/research/corpus
```

To build an incremental child, pass the complete parent revision during the
inventory step:

```bash
uv run kbd-corpus inventory \
  --term 22 \
  --parent-revision "$KBD_RESEARCH_CORPUS_REVISION" \
  --checkpoint .state/corpus-22-next.json
```

The child retains parent scopes outside the refreshed term, removes documents
no longer present in the new exact inventory, upserts current observations,
and reuses unaffected lexical shards. Use a new checkpoint for each new
inventory revision. `--refresh-metadata`, `--refresh-bill-indexes`, and
`--refresh-documents` force upstream re-observation when an operator is
investigating source changes.
