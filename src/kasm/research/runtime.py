"""Composition root for the hosted durable research workflow."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.adapters.korea.documents import BillDocumentsClient
from kasm.corpus import CorpusRepository, VercelBlobCorpusObjectStore
from kasm.search.terminology import TERMINOLOGY_VERSION

from .artifact_job_storage import ArtifactResearchJobStore
from .artifacts import VercelBlobResearchArtifactStore
from .backend import DurableResearchBackend
from .bill_documents import OfficialBillDocumentDiscoverer
from .corpus_runtime import RevisionCorpusRecallProvider
from .credentials import ResearchCredentialCodec
from .document_worker import OfficialDocumentWorker
from .documents import VercelBlobOfficialDocumentStore
from .engine import CorpusRecallProvider, ResearchEngine
from .finalizer import ConnectedResearchFinalizer
from .partitioning import ResearchPartitionPlanner
from .planner import ResearchContractPlanner
from .queue import VercelResearchTaskQueue
from .resolver import MetadataCandidateResolver
from .status_storage import StatusSnapshotResearchRunStore


@dataclass(frozen=True, slots=True)
class HostedResearchRuntime:
    engine: ResearchEngine
    backend: DurableResearchBackend
    artifacts: VercelBlobResearchArtifactStore
    documents: VercelBlobOfficialDocumentStore


def create_hosted_research_runtime(
    *,
    assembly_api_key_provider: Callable[[], str | None],
    corpus_recall_provider: CorpusRecallProvider | None = None,
) -> HostedResearchRuntime:
    """Build lazy hosted adapters without performing network I/O."""

    credential_secret = (
        os.getenv("KBD_RESEARCH_CREDENTIAL_SECRET", "").strip()
        or os.getenv("KBD_REMOTE_TOKEN_SECRET", "").strip()
    )
    if not credential_secret:
        raise RuntimeError("KBD_RESEARCH_CREDENTIAL_SECRET is required")

    parser_version = os.getenv("KBD_RESEARCH_PARSER_VERSION", "pypdf-layout-v1").strip()
    if not parser_version:
        raise RuntimeError("KBD_RESEARCH_PARSER_VERSION must not be empty")
    index_revision = os.getenv(
        "KBD_RESEARCH_INDEX_REVISION",
        f"research-v1+terms-{TERMINOLOGY_VERSION}+parser-{parser_version}",
    ).strip()
    build_sha = (
        os.getenv("VERCEL_GIT_COMMIT_SHA", "").strip()
        or os.getenv("KBD_BUILD_SHA", "").strip()
        or "local-uncommitted"
    )
    artifacts = VercelBlobResearchArtifactStore(
        prefix=os.getenv("KBD_RESEARCH_ARTIFACT_PREFIX", "kbd/research/artifacts")
    )
    documents = VercelBlobOfficialDocumentStore(
        prefix=os.getenv("KBD_RESEARCH_DOCUMENT_PREFIX", "kbd/research/documents")
    )
    queue = VercelResearchTaskQueue(
        topic=os.getenv("KBD_RESEARCH_QUEUE_TOPIC", "kbd-research"),
        region=os.getenv("KBD_RESEARCH_QUEUE_REGION") or None,
        timeout=float(os.getenv("KBD_RESEARCH_QUEUE_TIMEOUT_SECONDS", "10")),
    )

    api_timeout = float(os.getenv("KBD_RESEARCH_API_TIMEOUT_SECONDS", "30"))
    cache_root = Path(os.getenv("KBD_RESEARCH_CACHE_DIR", "/tmp/kbd-research"))
    resolved_corpus_provider = corpus_recall_provider
    if resolved_corpus_provider is None:
        corpus_revision = os.getenv("KBD_RESEARCH_CORPUS_REVISION", "").strip()
        if corpus_revision:
            corpus_repository = CorpusRepository(
                VercelBlobCorpusObjectStore(
                    prefix=os.getenv(
                        "KBD_RESEARCH_CORPUS_PREFIX",
                        "kbd/research/corpus",
                    )
                )
            )
            resolved_corpus_provider = RevisionCorpusRecallProvider(
                corpus_repository,
                revision_id=corpus_revision,
            )

    def page_client(api_key: str) -> AssemblyOpenApiClient:
        return AssemblyOpenApiClient(
            api_key,
            cache_dir=cache_root / "api-pages",
            timeout=api_timeout,
            cache_ttl_seconds=0,
        )

    engine = ResearchEngine(
        index_revision=index_revision,
        planner=ResearchContractPlanner(
            recent_months=int(os.getenv("KBD_RESEARCH_RECENT_MONTHS", "6"))
        ),
        partition_planner=ResearchPartitionPlanner(
            # Open Assembly accepts up to 1,000 rows per page.  Comprehensive
            # research intentionally scans complete metadata partitions, so
            # using the documented client maximum by default cuts user-key API
            # calls by up to 90% without dropping or sampling any candidates.
            page_size=int(os.getenv("KBD_RESEARCH_PAGE_SIZE", "1000"))
        ),
        jobs=ArtifactResearchJobStore(artifacts),
        queue=queue,
        credentials=ResearchCredentialCodec(credential_secret),
        page_client_factory=page_client,
        resolver=MetadataCandidateResolver(),
        bill_documents=OfficialBillDocumentDiscoverer(
            BillDocumentsClient(timeout=api_timeout)
        ),
        document_worker=OfficialDocumentWorker(
            documents,
            parser_version=parser_version,
            timeout=float(os.getenv("KBD_RESEARCH_DOCUMENT_TIMEOUT_SECONDS", "30")),
            max_bytes=int(
                os.getenv("KBD_RESEARCH_DOCUMENT_MAX_BYTES", str(50 * 1024 * 1024))
            ),
        ),
        finalizer=ConnectedResearchFinalizer(build_sha=build_sha),
        runs=StatusSnapshotResearchRunStore(
            artifacts,
            page_read_concurrency=int(
                os.getenv("KBD_RESEARCH_PAGE_READ_CONCURRENCY", "8")
            ),
        ),
        status_page_size=int(os.getenv("KBD_RESEARCH_STATUS_PAGE_SIZE", "100")),
        direct_fanout_limit=int(os.getenv("KBD_RESEARCH_DIRECT_FANOUT_LIMIT", "4")),
        fanout_chunk_size=int(os.getenv("KBD_RESEARCH_FANOUT_CHUNK_SIZE", "4")),
        fanout_delay_seconds=int(
            os.getenv("KBD_RESEARCH_FANOUT_DELAY_SECONDS", "1")
        ),
        corpus_recall_provider=resolved_corpus_provider,
    )
    backend = DurableResearchBackend(
        engine,
        documents,
        assembly_api_key_provider=assembly_api_key_provider,
    )
    return HostedResearchRuntime(engine, backend, artifacts, documents)


__all__ = ["HostedResearchRuntime", "create_hosted_research_runtime"]
