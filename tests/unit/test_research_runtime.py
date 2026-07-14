from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from kasm.research.artifact_job_storage import ArtifactResearchJobStore
from kasm.research.artifact_run_storage import ArtifactResearchRunStore
from kasm.research.backend import DurableResearchBackend
from kasm.research.corpus_runtime import RevisionCorpusRecallProvider
from kasm.research.runtime import create_hosted_research_runtime
from kasm.research.status_storage import StatusSnapshotResearchRunStore


def test_hosted_runtime_is_composed_lazily_without_network_or_user_key(monkeypatch) -> None:
    monkeypatch.setenv("KBD_RESEARCH_CREDENTIAL_SECRET", Fernet.generate_key().decode())
    requested = 0

    def api_key():
        nonlocal requested
        requested += 1
        return "user-key"

    runtime = create_hosted_research_runtime(assembly_api_key_provider=api_key)

    assert requested == 0
    assert isinstance(runtime.backend, DurableResearchBackend)
    assert isinstance(runtime.engine.jobs, ArtifactResearchJobStore)
    assert isinstance(runtime.engine.runs, ArtifactResearchRunStore)
    assert isinstance(runtime.engine.runs, StatusSnapshotResearchRunStore)
    assert runtime.engine.index_revision.startswith("research-v1+")
    assert runtime.engine.partition_planner.page_size == 1000
    assert runtime.engine.direct_fanout_limit == 4
    assert runtime.engine.fanout_chunk_size == 8
    assert runtime.engine.fanout_delay_seconds == 0


def test_hosted_runtime_allows_explicit_smaller_metadata_pages(monkeypatch) -> None:
    monkeypatch.setenv("KBD_RESEARCH_CREDENTIAL_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("KBD_RESEARCH_PAGE_SIZE", "250")

    runtime = create_hosted_research_runtime(
        assembly_api_key_provider=lambda: "user-key"
    )

    assert runtime.engine.partition_planner.page_size == 250


def test_hosted_runtime_requires_an_encryption_secret(monkeypatch) -> None:
    monkeypatch.delenv("KBD_RESEARCH_CREDENTIAL_SECRET", raising=False)
    monkeypatch.delenv("KBD_REMOTE_TOKEN_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="KBD_RESEARCH_CREDENTIAL_SECRET"):
        create_hosted_research_runtime(assembly_api_key_provider=lambda: None)


def test_hosted_runtime_composes_revision_bound_blob_corpus_from_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KBD_RESEARCH_CREDENTIAL_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("KBD_RESEARCH_CORPUS_REVISION", "a" * 64)
    monkeypatch.setenv("KBD_RESEARCH_CORPUS_PREFIX", "private/kbd-corpus")

    runtime = create_hosted_research_runtime(
        assembly_api_key_provider=lambda: "user-key"
    )

    provider = runtime.engine.corpus_recall_provider
    assert isinstance(provider, RevisionCorpusRecallProvider)
    assert provider.revision_id == "a" * 64
    assert runtime.engine.index_revision.endswith(provider.binding_id)
