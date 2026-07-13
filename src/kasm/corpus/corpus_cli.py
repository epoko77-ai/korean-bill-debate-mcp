"""Operator CLI for inventorying, building, and activating corpus revisions."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.adapters.korea.documents import BillDocumentsClient
from kasm.research.document_worker import OfficialDocumentWorker
from kasm.research.documents import FilesystemOfficialDocumentStore

from .build_pipeline import (
    CorpusBuildCheckpoint,
    CorpusBuildError,
    CorpusBuildRunner,
    publish_complete_revision,
    read_checkpoint,
    write_activation,
    write_checkpoint,
    write_complete_revision_manifest,
)
from .inventory import (
    OpenAssemblyCorpusInventorySource,
    finish_inventory_session,
    pin_inventory_session,
    write_inventory_manifest,
)
from .models import CorpusRevisionManifest
from .repository import CorpusRepository
from .storage import FilesystemCorpusObjectStore, VercelBlobCorpusObjectStore

DEFAULT_PARSER_VERSION: Final = "pypdf-6+kbd-corpus-v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kbd-corpus",
        description=(
            "Build an exhaustive, revisioned Assembly full-text corpus without "
            "serializing API credentials."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser(
        "inventory",
        help="enumerate a complete term without downloading PDFs",
    )
    inventory.add_argument("--term", type=int, action="append", required=True)
    inventory.add_argument("--checkpoint", type=Path, required=True)
    inventory.add_argument("--manifest-output", type=Path)
    inventory.add_argument("--discovery-cache-dir", type=Path)
    inventory.add_argument("--api-cache-dir", type=Path)
    inventory.add_argument("--parser-version", default=DEFAULT_PARSER_VERSION)
    inventory.add_argument("--parent-revision")
    inventory.add_argument("--page-size", type=int, default=1000)
    inventory.add_argument("--api-timeout", type=float, default=30.0)
    inventory.add_argument("--bill-index-timeout", type=float, default=30.0)
    inventory.add_argument("--as-of", type=_aware_datetime)
    inventory.add_argument("--refresh-metadata", action="store_true")
    inventory.add_argument("--refresh-bill-indexes", action="store_true")
    inventory.add_argument(
        "--dry-run",
        action="store_true",
        help="write only the inventory manifest; do not create or change build state",
    )

    status = commands.add_parser("status", help="show fail-closed checkpoint accounting")
    status.add_argument("--checkpoint", type=Path, required=True)

    build = commands.add_parser(
        "build",
        help="download and parse every pending inventory document",
    )
    _add_build_paths(build)
    build.add_argument("--attempts-per-item", type=int, default=3)
    build.add_argument("--document-timeout", type=float, default=60.0)
    build.add_argument("--retry-permanent", action="store_true")
    build.add_argument("--refresh-documents", action="store_true")

    publish = commands.add_parser(
        "publish",
        help="publish and optionally activate only a fully complete checkpoint",
    )
    _add_repository_args(publish)
    publish.add_argument("--checkpoint", type=Path, required=True)
    publish.add_argument("--revision-manifest-output", type=Path)
    publish.add_argument("--activation-output", type=Path)

    run = commands.add_parser(
        "run",
        help="resume all document work and publish only if it becomes complete",
    )
    _add_build_paths(run)
    run.add_argument("--attempts-per-item", type=int, default=3)
    run.add_argument("--document-timeout", type=float, default=60.0)
    run.add_argument("--retry-permanent", action="store_true")
    run.add_argument("--refresh-documents", action="store_true")
    run.add_argument("--revision-manifest-output", type=Path)
    run.add_argument("--activation-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            return _inventory(args, parser)
        if args.command == "status":
            return _status(args)
        if args.command == "build":
            return _build(args, publish=False)
        if args.command == "publish":
            return _publish(args)
        if args.command == "run":
            return _build(args, publish=True)
        parser.error("unsupported corpus command")
    except CorpusBuildError as exc:
        _print({"ok": False, "code": exc.code, "message": str(exc)}, error=True)
        return 2
    except KeyboardInterrupt:
        _print(
            {
                "ok": False,
                "code": "interrupted",
                "message": "progress through the previous document was checkpointed",
            },
            error=True,
        )
        return 130
    except Exception:
        # Never echo arbitrary upstream exception text: it may contain a URL or
        # credential supplied by a remote source.  Stable state remains on disk
        # for inspection through the status command.
        _print(
            {
                "ok": False,
                "code": "operation_failed",
                "message": "operation failed; inspect checkpoint accounting and retry",
            },
            error=True,
        )
        return 1
    return 0


def _inventory(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.dry_run and args.manifest_output is None:
        parser.error("inventory --dry-run requires --manifest-output")
    discovery_cache = args.discovery_cache_dir or args.checkpoint.with_suffix(
        args.checkpoint.suffix + ".inventory-cache"
    )
    as_of = pin_inventory_session(
        discovery_cache,
        requested_as_of=args.as_of,
    )
    api_cache = args.api_cache_dir or discovery_cache / "open-assembly-pages"
    api = AssemblyOpenApiClient(
        cache_dir=api_cache,
        timeout=args.api_timeout,
    )
    source = OpenAssemblyCorpusInventorySource(
        api,
        bill_documents=BillDocumentsClient(timeout=args.bill_index_timeout),
        page_size=args.page_size,
    )
    manifest = source.collect(
        args.term,
        inventory_as_of=as_of,
        discovery_cache_dir=discovery_cache,
        refresh_metadata=args.refresh_metadata,
        refresh_bill_indexes=args.refresh_bill_indexes,
    )
    finish_inventory_session(discovery_cache, manifest)
    if args.manifest_output is not None:
        write_inventory_manifest(args.manifest_output, manifest)
    if not args.dry_run:
        existing = read_checkpoint(args.checkpoint) if args.checkpoint.exists() else None
        checkpoint = CorpusBuildCheckpoint.merge_inventory(
            manifest,
            parser_version=args.parser_version,
            existing=existing,
            parent_revision_id=args.parent_revision,
        )
        write_checkpoint(args.checkpoint, checkpoint)
        summary: dict[str, Any] = checkpoint.summary()
    else:
        expected = (
            None
            if any(item.expected_count is None for item in manifest.coverage)
            else sum(item.expected_count or 0 for item in manifest.coverage)
        )
        summary = {
            "inventory_id": manifest.inventory_id,
            "inventory_complete": manifest.complete,
            "documents_expected": expected,
            "documents_scheduled": len(manifest.items),
            "inventory_gaps": len(manifest.gaps),
            "coverage": [item.to_dict() for item in manifest.coverage],
        }
    _print({"ok": True, "command": "inventory", **summary})
    return 0 if manifest.complete else 3


def _status(args: argparse.Namespace) -> int:
    checkpoint = read_checkpoint(args.checkpoint)
    _print({"ok": True, "command": "status", **checkpoint.summary()})
    return 0 if checkpoint.complete else 3


def _build(args: argparse.Namespace, *, publish: bool) -> int:
    checkpoint = read_checkpoint(args.checkpoint)
    repository = _repository(args)
    worker = OfficialDocumentWorker(
        FilesystemOfficialDocumentStore(args.document_cache_dir),
        parser_version=checkpoint.parser_version,
        timeout=args.document_timeout,
    )
    runner = CorpusBuildRunner(
        repository,
        worker,
        checkpoint_writer=lambda value: write_checkpoint(args.checkpoint, value),
    )
    checkpoint = runner.run(
        checkpoint,
        attempts_per_item=args.attempts_per_item,
        retry_permanent=args.retry_permanent,
        refresh_documents=args.refresh_documents,
    )
    write_checkpoint(args.checkpoint, checkpoint)
    if not publish or not checkpoint.complete:
        _print({"ok": True, "command": "build", **checkpoint.summary()})
        return 0 if checkpoint.complete else 3
    manifest = publish_complete_revision(repository, checkpoint)
    _export_publish_outputs(args, checkpoint, manifest)
    _print(
        {
            "ok": True,
            "command": "run",
            "complete": True,
            "revision_id": manifest.revision_id,
            "inventory_id": checkpoint.inventory.inventory_id,
        }
    )
    return 0


def _publish(args: argparse.Namespace) -> int:
    checkpoint = read_checkpoint(args.checkpoint)
    repository = _repository(args)
    manifest = publish_complete_revision(repository, checkpoint)
    _export_publish_outputs(args, checkpoint, manifest)
    _print(
        {
            "ok": True,
            "command": "publish",
            "complete": True,
            "revision_id": manifest.revision_id,
            "inventory_id": checkpoint.inventory.inventory_id,
        }
    )
    return 0


def _export_publish_outputs(
    args: argparse.Namespace,
    checkpoint: CorpusBuildCheckpoint,
    manifest: CorpusRevisionManifest,
) -> None:
    if args.revision_manifest_output is not None:
        write_complete_revision_manifest(args.revision_manifest_output, manifest)
    if args.activation_output is not None:
        write_activation(
            args.activation_output,
            manifest,
            inventory_id=checkpoint.inventory.inventory_id,
        )


def _add_build_paths(parser: argparse.ArgumentParser) -> None:
    _add_repository_args(parser)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--document-cache-dir", type=Path, required=True)


def _add_repository_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--storage",
        choices=("filesystem", "vercel-blob"),
        default="filesystem",
    )
    parser.add_argument("--corpus-dir", type=Path)
    parser.add_argument("--blob-prefix", default="kbd/research/corpus")


def _repository(args: argparse.Namespace) -> CorpusRepository:
    if args.storage == "vercel-blob":
        return CorpusRepository(VercelBlobCorpusObjectStore(prefix=args.blob_prefix))
    if args.corpus_dir is None:
        raise CorpusBuildError(
            "--corpus-dir is required for filesystem storage",
            code="corpus_directory_required",
        )
    return CorpusRepository(FilesystemCorpusObjectStore(args.corpus_dir))


def _aware_datetime(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--as-of must be an ISO datetime") from exc
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("--as-of must include a UTC offset")
    return value


def _print(payload: Mapping[str, Any], *, error: bool = False) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=sys.stderr if error else sys.stdout,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["DEFAULT_PARSER_VERSION", "build_parser", "main"]
