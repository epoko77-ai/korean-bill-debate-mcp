from kasm.app import create_services
from kasm.mcp.tools import KasmTools


def test_sample_app_search_and_context() -> None:
    tools = KasmTools(create_services())
    result = tools.search_speeches("domestic foundation models", include_context=True)
    assert result["results"]
    assert result["results"][0]["official_source"]
    speech_id = result["results"][0]["speech_id"]
    speech = tools.get_speech(speech_id)
    assert speech["id"] == speech_id
    assert speech["official_source"]
    assert speech["source_locator"]
    assert speech["meeting_source_hash"]
    assert speech["retrieved_at"]
    context = tools.get_speech_context(speech_id, before=1, after=1)
    assert context["speeches"]
    assert context["relations"]


def test_sample_catalog() -> None:
    tools = KasmTools(create_services())
    assert tools.list_committees(assembly_term=22)[0]["committee_id"] == "science-ict"
    assert tools.list_meetings(committee="과학기술정보방송통신위원회")
