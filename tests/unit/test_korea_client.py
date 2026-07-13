import hashlib
import json
import urllib.parse

import pytest

from kasm import __version__
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


def api_payload(dataset: str, total_count: int, rows: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            dataset: [
                {
                    "head": [
                        {"list_total_count": total_count},
                        {"RESULT": {"CODE": "INFO-000"}},
                    ]
                },
                {"row": rows},
            ]
        },
        ensure_ascii=False,
    ).encode()


@pytest.fixture
def three_page_api():
    dataset = "allbill"
    rows = [{"BILL_NO": f"{index:07d}", "TITLE": f"법안 {index}"} for index in range(237)]
    payloads = {
        page: api_payload(dataset, 237, rows[(page - 1) * 100 : page * 100]) for page in range(1, 4)
    }
    calls: list[int] = []

    def opener(request, **_kwargs):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        page = int(query["pIndex"][0])
        calls.append(page)
        return Response(payloads[page])

    return AssemblyOpenApiClient("top-secret", opener=opener), dataset, rows, payloads, calls


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
    assert calls[0].get_header("User-agent") == (
        f"Mozilla/5.0 (compatible; KASM/{__version__})"
    )


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


@pytest.mark.parametrize(
    ("code", "message"),
    (
        ("ERROR-290", "인증키가 유효하지 않습니다."),
        ("ERROR-300", "필수 값이 누락되었습니다."),
    ),
)
def test_top_level_api_error_preserves_code_and_message(code, message) -> None:
    payload = json.dumps({"RESULT": {"CODE": code, "MESSAGE": message}}).encode()
    client = AssemblyOpenApiClient(
        "private-key", opener=lambda *_args, **_kwargs: Response(payload)
    )

    with pytest.raises(AssemblyApiError) as raised:
        client.fetch_page("dataset")

    assert str(raised.value) == f"{code}: {message}"
    assert "Unexpected Open Assembly response schema" not in str(raised.value)


def test_api_error_diagnostics_are_bounded_and_do_not_leak_key() -> None:
    payload = json.dumps(
        {
            "RESULT": {
                "CODE": "ERROR-290-private-key",
                "MESSAGE": "private-key " + ("x" * 1_000),
            }
        }
    ).encode()
    client = AssemblyOpenApiClient(
        "private-key", opener=lambda *_args, **_kwargs: Response(payload)
    )

    with pytest.raises(AssemblyApiError) as raised:
        client.fetch_page("dataset")

    assert "private-key" not in str(raised.value)
    assert "***" in str(raised.value)
    assert len(str(raised.value)) <= 64 + 2 + 500


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


def test_fetch_all_follows_total_count_and_preserves_page_provenance(
    three_page_api,
) -> None:
    client, dataset, expected_rows, payloads, calls = three_page_api

    result = client.fetch_all(
        dataset,
        page_size=100,
        parameters={"BILL_NAME": "인공지능", "AGE": 22},
    )

    assert calls == [1, 2, 3]
    assert result.total_count == 237
    assert list(result.rows) == expected_rows
    assert [page.page for page in result.pages] == [1, 2, 3]
    assert result.source_hashes == tuple(
        hashlib.sha256(payloads[page]).hexdigest() for page in range(1, 4)
    )
    assert len(result.source_hash) == 64
    assert all("top-secret" not in url for url in result.source_urls)
    assert all("KEY=%2A%2A%2A" in url for url in result.source_urls)
    page_indexes = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["pIndex"][0]
        for url in result.source_urls
    ]
    assert page_indexes == ["1", "2", "3"]
    assert "top-secret" not in repr(result)


def test_iter_pages_yields_every_page_in_order(three_page_api) -> None:
    client, dataset, _rows, _payloads, calls = three_page_api

    pages = tuple(client.iter_pages(dataset, page_size=100))

    assert calls == [1, 2, 3]
    assert [len(page.rows) for page in pages] == [100, 100, 37]


def test_fetch_all_rejects_an_empty_page_before_total_count() -> None:
    dataset = "allbill"
    first_rows = [{"BILL_NO": f"{index:07d}"} for index in range(100)]

    def opener(request, **_kwargs):
        page = int(
            urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["pIndex"][0]
        )
        rows = first_rows if page == 1 else []
        return Response(api_payload(dataset, 237, rows))

    client = AssemblyOpenApiClient("key", opener=opener)
    with pytest.raises(AssemblyApiError, match="page 2: expected 100 rows but received 0"):
        client.fetch_all(dataset, page_size=100)


def test_fetch_all_rejects_an_entire_repeated_page() -> None:
    dataset = "allbill"
    repeated = [{"BILL_NO": f"{index:07d}"} for index in range(100)]

    def opener(request, **_kwargs):
        page = int(
            urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["pIndex"][0]
        )
        rows = repeated if page < 3 else [{"BILL_NO": f"x{index:06d}"} for index in range(37)]
        return Response(api_payload(dataset, 237, rows))

    client = AssemblyOpenApiClient("key", opener=opener)
    with pytest.raises(AssemblyApiError, match="page 2: received a repeated page"):
        client.fetch_all(dataset, page_size=100)


def test_fetch_all_preserves_legitimate_duplicate_rows_within_a_page() -> None:
    dataset = "allbill"
    rows = [
        {"BILL_NO": "2200001", "AGENDA": "같은 공개 레코드"},
        {"BILL_NO": "2200001", "AGENDA": "같은 공개 레코드"},
        {"BILL_NO": "2200002", "AGENDA": "다른 공개 레코드"},
    ]
    client = AssemblyOpenApiClient(
        "key", opener=lambda *_args, **_kwargs: Response(api_payload(dataset, 3, rows))
    )

    result = client.fetch_all(dataset, page_size=100)

    assert list(result.rows) == rows
    assert result.total_count == 3


def test_fetch_all_rejects_total_count_drift() -> None:
    dataset = "allbill"

    def opener(request, **_kwargs):
        page = int(
            urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["pIndex"][0]
        )
        start = (page - 1) * 100
        rows = [{"BILL_NO": f"{index:07d}"} for index in range(start, start + 100)]
        return Response(api_payload(dataset, 237 if page == 1 else 238, rows))

    client = AssemblyOpenApiClient("key", opener=opener)
    with pytest.raises(AssemblyApiError, match="total_count changed from 237 to 238"):
        client.fetch_all(dataset, page_size=100)


def test_fetch_all_reports_page_failure_without_leaking_key() -> None:
    dataset = "allbill"
    first_rows = [{"BILL_NO": f"{index:07d}"} for index in range(100)]

    def opener(request, **_kwargs):
        page = int(
            urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["pIndex"][0]
        )
        if page == 2:
            raise OSError("connection failed for api-key-secret")
        return Response(api_payload(dataset, 237, first_rows))

    client = AssemblyOpenApiClient("api-key-secret", opener=opener)
    with pytest.raises(AssemblyApiError) as raised:
        client.fetch_all(dataset, page_size=100)

    assert "page 2" in str(raised.value)
    assert "api-key-secret" not in str(raised.value)


def test_parameters_cannot_override_pagination_or_credentials() -> None:
    client = AssemblyOpenApiClient("key", opener=lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="must not override"):
        client.fetch_page("allbill", parameters={"pIndex": 7})
    with pytest.raises(ValueError, match="must not override"):
        client.fetch_page("allbill", parameters={"key": "not-the-real-key"})
