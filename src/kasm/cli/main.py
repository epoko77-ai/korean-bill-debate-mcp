"""Standard-library CLI for Korean Assembly speech search."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, cast

from kasm.mcp.tools import KasmTools, ServiceContext, to_jsonable

ServiceFactory = Callable[[], ServiceContext]


def _service_factory() -> ServiceContext:
    """Load the application factory without coupling the CLI to storage details."""

    reference = os.getenv("KASM_SERVICE_FACTORY", "kasm.app:create_auto_services")
    module_name, separator, attribute = reference.partition(":")
    if not separator:
        raise RuntimeError("KASM_SERVICE_FACTORY must use the form module:function")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Kasm services are not configured. Set KASM_SERVICE_FACTORY=module:function "
            "or install a package containing kasm.app:create_services."
        ) from exc
    return cast(ServiceContext, factory())


def _print(value: Any) -> None:
    print(json.dumps(to_jsonable(value), ensure_ascii=False, indent=2))


def _print_demo(payload: dict[str, Any], graph: dict[str, Any]) -> None:
    results = payload.get("results", [])
    print("Korean Bill & Debate MCP — 오프라인 데모")
    print("명시적 합성 샘플 · API 키 없음 · 네트워크 없음\n")
    print(f"질문: {payload['query']}\n")
    for rank, item in enumerate(results[:2], 1):
        print(f"{rank}. {item['speaker']} · {item.get('speaker_role') or '역할 미상'}")
        print(f"   {item['text']}")
        if item.get("context_before"):
            print(f"   ← {item['context_before']}")
        if item.get("context_after"):
            print(f"   → {item['context_after']}")
        print(f"   검색: 어휘 순위 #{item['lexical_rank']} · 앞뒤 문맥 복원")
        print()
    bills = graph.get("bills", [])
    if bills:
        bill = bills[0]
        print("연결된 근거 흐름")
        print(f"   정책 → 의안 {bill['bill_no']} · {bill['name']}")
        print(f"   의안 → {bill['committee']} · 상태: {bill['status']}")
        print("   의안 → 위원회 회의 → 의원 발언 → 앞뒤 질의·답변\n")
    print("실데이터: 한·영 E5 + FTS5 + RRF 검색, 공식 회의록 출처 제공")


def _print_research(result: dict[str, Any]) -> None:
    """Render the connected evidence trail as a readable terminal report."""
    print("Korean Bill & Debate MCP — 법안·논의 시계열 조사\n")
    print(f"질문: {result['query']}\n")
    print("시계열")
    timeline = result.get("timeline", [])
    dated_bills: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for event in timeline:
        if event["event_type"] != "debate" and event["date"] not in seen_dates:
            dated_bills.append(event)
            seen_dates.add(event["date"])
    if len(dated_bills) > 4:
        last = len(dated_bills) - 1
        dated_bills = [dated_bills[index] for index in (0, last // 3, last * 2 // 3, last)]
    debates = [event for event in timeline if event["event_type"] == "debate"]
    shown = [*dated_bills, *(debates[:1])]
    for event in shown:
        label = "토론" if event["event_type"] == "debate" else "의안"
        print(f"  {event['date']}  [{label}] {event['detail']}")
        if event.get("bill_no"):
            print(f"              의안 {event['bill_no']} · {event['title']}")
        if event.get("participants"):
            print(f"              참여: {' · '.join(event['participants'][:6])}")
    print("\n관련 발언과 앞뒤 맥락")
    seen_speakers: set[str] = set()
    for speech in result.get("speeches", []):
        if speech["speaker"] in seen_speakers:
            continue
        seen_speakers.add(speech["speaker"])
        text = " ".join(speech["text"].split())
        print(f"  • {speech['speaker']} · {speech.get('committee') or '위원회 미상'}")
        print(f"    {text[:105]}{'…' if len(text) > 105 else ''}")
        print(f"    원문: {speech['citation']['official_url']}")
        if len(seen_speakers) >= 5:
            break
    quality = result["quality"]
    print(
        "\n근거 요약: "
        f"의안 {len(result['bills'])}건 · 발언 {quality['speech_matches']}건 · "
        f"토론 스레드 {quality['discussion_threads']}개 · 앞뒤 발언 {quality['context_turns']}개"
    )
    print(f"공식 출처 보존율: {quality['provenance_rate']:.0%}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kbd", description="Search Korean Assembly speeches")
    subcommands = parser.add_subparsers(dest="command", required=True)

    setup = subcommands.add_parser("setup", help="store an Open Assembly key and register MCP")
    setup.add_argument(
        "--client",
        required=True,
        choices=("claude-code", "codex", "gemini", "claude-desktop"),
    )
    setup.add_argument("--api-key", help=argparse.SUPPRESS)
    setup.add_argument("--credentials-file")
    setup.add_argument("--no-validate", action="store_true")

    demo = subcommands.add_parser("demo", help="run a search against the bundled sample data")
    demo.add_argument(
        "query",
        nargs="?",
        default="해외 기반 모델 의존에 대한 우려와 정부 답변",
    )

    search = subcommands.add_parser("search", help="search speeches")
    search.add_argument("query")
    search.add_argument("--assembly-term", type=int)
    search.add_argument("--committee")
    search.add_argument("--speaker")
    search.add_argument("--speaker-role")
    search.add_argument("--organization")
    search.add_argument("--meeting-type")
    search.add_argument("--date-from")
    search.add_argument("--date-to")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--no-context", action="store_true")
    search.add_argument("--database")
    search.add_argument("--vector-index")

    research = subcommands.add_parser(
        "research", help="connect bills, status, debates, and a dated evidence timeline"
    )
    research.add_argument("query")
    research.add_argument("--limit", type=int, default=12)
    research.add_argument("--database")
    research.add_argument("--vector-index")

    inspect = subcommands.add_parser("inspect", help="show one speech and its provenance")
    inspect.add_argument("speech_id")

    sync = subcommands.add_parser("sync", help="fetch official transcript metadata")
    sync.add_argument("--assembly-term", type=int)
    sync.add_argument("--committee")
    sync.add_argument(
        "--source", choices=("committee", "plenary", "subcommittee"), default="committee"
    )
    sync.add_argument("--dataset", help="override the verified Open Assembly service code")
    sync.add_argument("--month", help="meeting month in YYYY-MM form")
    sync.add_argument("--page-size", type=int, default=100)
    sync.add_argument("--page", type=int, default=1)
    sync.add_argument("--all-pages", action="store_true")
    sync.add_argument("--refresh", action="store_true")
    sync.add_argument("--database", default="kasm.sqlite3")
    sync.add_argument("--cache-dir", default=".kasm-cache")
    sync.add_argument("--max-meetings", type=int, default=1)
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--ingest", action="store_true")

    bills = subcommands.add_parser("sync-bills", help="sync official bills and agendas")
    bills.add_argument("--assembly-term", type=int, default=22)
    bills.add_argument("--query")
    bills.add_argument("--dataset", default="nzmimeepazxkubdpn")
    bills.add_argument("--status-dataset", default="nwbpacrgavhjryiph")
    bills.add_argument("--no-status", action="store_true")
    bills.add_argument("--page-size", type=int, default=100)
    bills.add_argument("--page", type=int, default=1)
    bills.add_argument("--all-pages", action="store_true")
    bills.add_argument("--refresh", action="store_true")
    bills.add_argument("--database", default="kasm.sqlite3")
    bills.add_argument("--cache-dir", default=".kasm-cache")

    index = subcommands.add_parser("index", help="build the local semantic index")
    index.add_argument("--embedding-model", default="intfloat/multilingual-e5-small")
    index.add_argument("--database", default="kasm.sqlite3")
    index.add_argument("--output", default="kasm-vectors.json")
    index.add_argument("--backend", choices=("exact", "faiss"), default="exact")

    mcp = subcommands.add_parser("mcp", help="run the MCP server")
    mcp.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8000)
    mcp.add_argument("--database")
    mcp.add_argument("--vector-index")
    return parser


def main(
    argv: Sequence[str] | None = None, *, service_factory: ServiceFactory | None = None
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "setup":
        from kasm.setup import run_setup

        try:
            setup_result = run_setup(
                client_name=args.client,
                api_key=args.api_key,
                credentials_file=args.credentials_file,
                validate=not args.no_validate,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"kbd: {exc}", file=sys.stderr)
            return 2
        _print(setup_result)
        return 0
    if args.command == "sync-bills":
        from kasm.adapters.korea.bills import ingest_bill_rows
        from kasm.adapters.korea.client import AssemblyOpenApiClient
        from kasm.storage.database import Database

        parameters: dict[str, str | int] = {"AGE": args.assembly_term}
        if args.query:
            parameters["BILL_NAME"] = args.query
        try:
            client = AssemblyOpenApiClient(cache_dir=args.cache_dir)
            first = client.fetch_page(
                args.dataset,
                page=args.page,
                page_size=args.page_size,
                parameters=parameters,
                refresh=args.refresh,
            )
            pages = [first]
            if args.all_pages:
                total_pages = ((first.total_count or 0) + args.page_size - 1) // args.page_size
                pages.extend(
                    client.fetch_page(
                        args.dataset,
                        page=number,
                        page_size=args.page_size,
                        parameters=parameters,
                        refresh=args.refresh,
                    )
                    for number in range(2, total_pages + 1)
                )
            rows = tuple(row for page in pages for row in page.rows)
            status_pages = []
            if not args.no_status:
                status_first = client.fetch_page(
                    args.status_dataset,
                    page=1,
                    page_size=args.page_size,
                    parameters={"AGE": args.assembly_term},
                    refresh=args.refresh,
                )
                status_pages = [status_first]
                if args.all_pages:
                    status_total = (
                        (status_first.total_count or 0) + args.page_size - 1
                    ) // args.page_size
                    status_pages.extend(
                        client.fetch_page(
                            args.status_dataset,
                            page=number,
                            page_size=args.page_size,
                            parameters={"AGE": args.assembly_term},
                            refresh=args.refresh,
                        )
                        for number in range(2, status_total + 1)
                    )
            status_rows = tuple(row for page in status_pages for row in page.rows)
            hashes = [page.source_hash for page in [*pages, *status_pages]]
            source_hash = hashlib.sha256("".join(hashes).encode()).hexdigest()
            with Database(args.database) as database:
                saved = ingest_bill_rows(database, rows, source_hash=source_hash)
                statuses_saved = ingest_bill_rows(database, status_rows, source_hash=source_hash)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"kbd: {exc}", file=sys.stderr)
            return 2
        _print(
            {
                "dataset": args.dataset,
                "rows": len(rows),
                "saved": saved,
                "status_rows": len(status_rows),
                "statuses_saved": statuses_saved,
                "pages_fetched": len(pages),
                "status_pages_fetched": len(status_pages),
                "database": args.database,
                "source_url": first.source_url,
            }
        )
        return 0
    if args.command == "sync":
        from kasm.adapters.korea.client import AssemblyOpenApiClient
        from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource, classify_meeting

        env_name = f"ASSEMBLY_{args.source.upper()}_DATASET"
        source = MeetingSource(args.source)
        dataset = args.dataset or os.getenv(env_name) or DATASET_BY_SOURCE[source]
        if source is not MeetingSource.SUBCOMMITTEE and not args.month:
            print(
                "kbd: --month YYYY-MM is required for plenary and committee sync",
                file=sys.stderr,
            )
            return 2
        if args.all_pages and args.page != 1:
            print("kbd: --all-pages requires --page 1", file=sys.stderr)
            return 2
        if source is MeetingSource.SUBCOMMITTEE:
            parameters = {"ERACO": f"제{args.assembly_term}대"} if args.assembly_term else {}
            if args.committee:
                parameters["CMIT_CD"] = args.committee
        else:
            parameters = {
                "DAE_NUM": args.assembly_term,
                "CONF_DATE": args.month,
            }
            if args.committee:
                parameters["COMM_NAME"] = args.committee
        try:
            client = AssemblyOpenApiClient(cache_dir=args.cache_dir)
            page = client.fetch_page(
                dataset,
                page=args.page,
                page_size=args.page_size,
                parameters=parameters,
                refresh=args.refresh,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"kbd: {exc}", file=sys.stderr)
            return 2
        pages = [page]
        if args.all_pages:
            total_pages = (
                (page.total_count + args.page_size - 1) // args.page_size
                if page.total_count is not None
                else 1
            )
            try:
                pages.extend(
                    client.fetch_page(
                        dataset,
                        page=page_number,
                        page_size=args.page_size,
                        parameters=parameters,
                        refresh=args.refresh,
                    )
                    for page_number in range(2, total_pages + 1)
                )
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"kbd: {exc}", file=sys.stderr)
                return 2
        all_rows = tuple(row for current in pages for row in current.rows)
        counts: dict[str, int] = {}
        for row in all_rows:
            kind = classify_meeting(row).value
            counts[kind] = counts.get(kind, 0) + 1
        result: dict[str, Any] = {
            "dataset": page.dataset,
            "page": page.page,
            "pages_fetched": len(pages),
            "rows": len(all_rows),
            "total_count": page.total_count,
            "meeting_types": counts,
            "source_url": page.source_url,
            "source_hash": hashlib.sha256(
                "".join(current.source_hash for current in pages).encode()
            ).hexdigest(),
            "page_source_hashes": [current.source_hash for current in pages],
            "cached_in": args.cache_dir,
        }
        if args.dry_run or args.ingest:
            from kasm.adapters.korea.fetcher import MinutesFetcher
            from kasm.adapters.korea.pipeline import OpenAssemblyPipeline, distinct_minutes_rows
            from kasm.storage.database import Database

            if not 1 <= args.max_meetings <= 100:
                print("kbd: --max-meetings must be between 1 and 100", file=sys.stderr)
                return 2
            reports: list[Any] = []
            distinct_rows = distinct_minutes_rows(all_rows)
            with Database(args.database) as database:
                pipeline = OpenAssemblyPipeline(database, MinutesFetcher(args.cache_dir))
                for row in distinct_rows[: args.max_meetings]:
                    if args.ingest:
                        ingested = pipeline.sync(row, refresh=args.refresh)
                        reports.append(
                            {
                                "meeting_id": ingested.meeting.id,
                                "speeches_saved": ingested.speeches_saved,
                                "relations_saved": ingested.relations_saved,
                                "parse_failures": len(ingested.failures),
                            }
                        )
                    else:
                        reports.append(pipeline.preview(row, refresh=args.refresh))
            result["mode"] = "ingest" if args.ingest else "dry-run"
            result["reports"] = reports
            result["database"] = args.database
            result["duplicate_rows_skipped"] = len(all_rows) - len(distinct_rows)
        _print(result)
        return 0
    if args.command == "index":
        from kasm.indexing.build import build_vector_index
        from kasm.indexing.embeddings import HashEmbeddingProvider, SentenceTransformersProvider
        from kasm.storage.database import Database

        provider = (
            HashEmbeddingProvider()
            if args.embedding_model == "kasm/hash-token-v1"
            else SentenceTransformersProvider(args.embedding_model)
        )
        try:
            with Database(args.database) as database:
                metadata = build_vector_index(database, provider, args.output, backend=args.backend)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"kbd: {exc}", file=sys.stderr)
            return 2
        _print(metadata)
        return 0

    try:
        if (
            service_factory is None
            and args.command in {"search", "research", "mcp"}
            and args.database is not None
        ):
            from kasm.app import create_deployed_services

            services = create_deployed_services(args.database, args.vector_index)
        else:
            if service_factory is None and args.command == "demo":
                from kasm.app import create_services

                services = create_services()
            else:
                services = (service_factory or _service_factory)()
        tools = KasmTools(services)
        if args.command in {"demo", "search"}:
            kwargs = (
                {}
                if args.command == "demo"
                else {
                    "assembly_term": args.assembly_term,
                    "committee": args.committee,
                    "speaker": args.speaker,
                    "speaker_role": args.speaker_role,
                    "organization": args.organization,
                    "meeting_type": args.meeting_type,
                    "date_from": args.date_from,
                    "date_to": args.date_to,
                    "limit": args.limit,
                    "include_context": not args.no_context,
                }
            )
            payload = tools.search_speeches(args.query, **kwargs)
            _print_demo(
                payload, tools.explore_issue("인공지능")
            ) if args.command == "demo" else _print(payload)
        elif args.command == "inspect":
            _print(tools.get_speech(args.speech_id))
        elif args.command == "research":
            _print_research(tools.explore_issue(args.query, limit=args.limit))
        elif args.command == "mcp":
            from kasm.mcp.server import run

            run(services, transport=args.transport, host=args.host, port=args.port)
        return 0
    except (LookupError, RuntimeError, ValueError) as exc:
        print(f"kbd: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
