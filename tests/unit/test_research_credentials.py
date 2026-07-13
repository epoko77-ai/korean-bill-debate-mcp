import pytest
from cryptography.fernet import Fernet

from kasm.research.credentials import ResearchCredentialCodec


def _codec(now: list[float]) -> ResearchCredentialCodec:
    return ResearchCredentialCodec(Fernet.generate_key().decode(), now=lambda: now[0])


def test_capability_encrypts_and_binds_user_key_to_one_job_and_query() -> None:
    now = [1_000.0]
    codec = _codec(now)
    token = codec.issue(
        research_id="research_1",
        query_fingerprint="a" * 64,
        assembly_api_key="personal-open-assembly-key",
    )

    assert "personal-open-assembly-key" not in token
    credential = codec.reveal(
        token,
        research_id="research_1",
        query_fingerprint="a" * 64,
    )
    assert credential.assembly_api_key == "personal-open-assembly-key"
    assert credential.expires_at == 4_600.0


def test_capability_rejects_cross_job_and_cross_query_replay() -> None:
    now = [1_000.0]
    codec = _codec(now)
    token = codec.issue(
        research_id="research_1",
        query_fingerprint="a" * 64,
        assembly_api_key="key",
    )

    with pytest.raises(ValueError, match="another job"):
        codec.reveal(token, research_id="research_2", query_fingerprint="a" * 64)
    with pytest.raises(ValueError, match="another query"):
        codec.reveal(token, research_id="research_1", query_fingerprint="b" * 64)


def test_capability_expires_without_echoing_secret() -> None:
    now = [1_000.0]
    codec = _codec(now)
    token = codec.issue(
        research_id="research_1",
        query_fingerprint="a" * 64,
        assembly_api_key="never-echo-me",
        ttl_seconds=60,
    )
    now[0] = 1_061.0

    with pytest.raises(ValueError) as error:
        codec.reveal(token, research_id="research_1", query_fingerprint="a" * 64)
    assert "never-echo-me" not in str(error.value)


def test_capability_is_authenticated() -> None:
    now = [1_000.0]
    codec = _codec(now)

    with pytest.raises(ValueError, match="invalid or expired"):
        codec.reveal("g" * 120, research_id="research_1", query_fingerprint="a" * 64)
