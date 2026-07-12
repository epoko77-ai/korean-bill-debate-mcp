import json

import pytest

from kasm.adapters.korea.client import AssemblyApiError, AssemblyOpenApiClient


class Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self) -> bytes:
        return self.payload


def test_fetch_page_decodes_and_redacts_key(tmp_path) -> None:
    dataset = "committee123"
    payload = {
        dataset: [
            {"head": [{"list_total_count": 1}, {"RESULT": {"CODE": "INFO-000"}}]},
            {"row": [{"CONF_NAME": "과학기술정보방송통신위원회"}]},
        ]
    }
    calls = []

    def opener(url, **_):
        calls.append(url)
        return Response(json.dumps(payload, ensure_ascii=False).encode())

    client = AssemblyOpenApiClient("secret", cache_dir=tmp_path, opener=opener)
    page = client.fetch_page(dataset)
    cached = client.fetch_page(dataset)
    assert page.rows == cached.rows
    assert page.total_count == 1
    assert "secret" not in page.source_url
    assert len(calls) == 1
    assert calls[0].get_header("User-agent").startswith("Mozilla/5.0")


def test_key_and_dataset_are_validated(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AssemblyApiError, match="ASSEMBLY_OPEN_API_KEY"):
        AssemblyOpenApiClient(api_key="").fetch_page("dataset")
    with pytest.raises(ValueError, match="alphanumeric"):
        AssemblyOpenApiClient("key").fetch_page("../dataset")


def test_no_data_response_is_an_empty_success() -> None:
    payload = json.dumps(
        {"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}
    ).encode()
    client = AssemblyOpenApiClient("key", opener=lambda *_args, **_kwargs: Response(payload))
    page = client.fetch_page("dataset")
    assert page.total_count == 0
    assert page.rows == ()


def test_request_scoped_api_key_provider_overrides_placeholder() -> None:
    dataset = "dataset"
    payload = json.dumps(
        {
            dataset: [
                {"head": [{"list_total_count": 0}, {"RESULT": {"CODE": "INFO-000"}}]},
                {"row": []},
            ]
        }
    ).encode()
    requests = []

    def opener(request, **_kwargs):
        requests.append(request.full_url)
        return Response(payload)

    client = AssemblyOpenApiClient(
        "placeholder",
        api_key_provider=lambda: "personal-key",
        opener=opener,
    )
    client.fetch_page(dataset)

    assert "personal-key" in requests[0]
    assert "placeholder" not in requests[0]
