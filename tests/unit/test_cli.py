from __future__ import annotations

import json

from kasm.app import create_services
from kasm.cli.main import main
from kasm.mcp.tools import ServiceContext


class Services:
    def search(self, query, **filters):
        return [{"speech_id": "s1", "text": query, "limit": filters["limit"]}]

    def get_speech(self, speech_id):
        return {"id": speech_id, "text": "원문"}


def factory():
    service = Services()
    return ServiceContext(service, service)


def test_search_prints_utf8_json(capsys):
    assert main(["search", "소버린 AI", "--limit", "4"], service_factory=factory) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["results"][0] == {"speech_id": "s1", "text": "소버린 AI", "limit": 4}


def test_inspect_prints_speech(capsys):
    assert main(["inspect", "speech-7"], service_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "speech-7"


def test_index_and_sync_validation_do_not_initialize_services(capsys, tmp_path):
    def fail():
        raise AssertionError("must not initialize")

    assert main(["sync", "--assembly-term", "22"], service_factory=fail) == 2
    assert "--month YYYY-MM" in capsys.readouterr().err
    assert (
        main(
            [
                "index",
                "--embedding-model",
                "kasm/hash-token-v1",
                "--database",
                str(tmp_path / "empty.sqlite3"),
                "--output",
                str(tmp_path / "vectors.json"),
            ],
            service_factory=fail,
        )
        == 2
    )
    assert "without speeches" in capsys.readouterr().err
    assert (
        main(
            [
                "sync",
                "--source",
                "committee",
                "--month",
                "2025-01",
                "--all-pages",
                "--page",
                "2",
            ],
            service_factory=fail,
        )
        == 2
    )
    assert "--all-pages requires --page 1" in capsys.readouterr().err


def test_configuration_errors_are_friendly(capsys):
    def fail():
        raise RuntimeError("no database")

    assert main(["demo"], service_factory=fail) == 2
    assert "kbd: no database" in capsys.readouterr().err


def test_demo_is_concise_and_explicitly_synthetic(capsys):
    assert main(["demo"], service_factory=create_services) == 0
    output = capsys.readouterr().out
    assert "질문:" in output
    assert "명시적 합성 샘플" in output
    assert "앞뒤 문맥 복원" in output
    assert "연결된 근거 흐름" in output
    assert "상태: 계류" in output
    assert "공식 회의록 출처" in output
