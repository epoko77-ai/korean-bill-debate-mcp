"""Exercise the hosted-workspace research path without spending a user's LLM credits."""

from __future__ import annotations

import json
import os

from kasm.workspace.service import run_workspace_research


def exercise() -> dict[str, object]:
    if not os.getenv("ASSEMBLY_OPEN_API_KEY"):
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")
    captured: dict[str, object] = {}

    def verify_research(
        provider: str,
        api_key: str,
        question: str,
        research: dict[str, object],
    ) -> tuple[str, str]:
        del provider, api_key, question
        bills = research.get("bills")
        validation = research.get("bill_number_validation")
        if not isinstance(bills, list) or not bills:
            raise RuntimeError("workspace research returned no exact bill")
        if any(not isinstance(bill, dict) or bill.get("bill_no") != "2219564" for bill in bills):
            raise RuntimeError("workspace research substituted an unrelated bill")
        if not isinstance(validation, dict) or validation.get("exact_match") is not True:
            raise RuntimeError("workspace exact bill validation failed")
        captured["bill_name"] = bills[0].get("name")
        captured["bill_proposed_at"] = bills[0].get("proposed_at")
        captured["bill_count"] = len(bills)
        captured["speech_count"] = len(research.get("speeches") or [])
        captured["thread_count"] = len(research.get("discussion_threads") or [])
        captured["evidence_characters"] = len(json.dumps(research, ensure_ascii=False))
        refresh = research.get("live_refresh")
        if isinstance(refresh, dict):
            captured["minutes_ingested"] = refresh.get("minutes_ingested")
            captured["months_queried"] = len(refresh.get("months_queried") or [])
        return "장문 합성 직전까지 검증됨", "fixture-no-llm-spend"

    result = run_workspace_research(
        question="의안번호 2219564 보완수사권의 발의부터 현재까지 쟁점과 발언",
        assembly_api_key=os.environ["ASSEMBLY_OPEN_API_KEY"],
        llm_provider="anthropic",
        llm_api_key="not-sent-to-provider",
        synthesizer=verify_research,
    )
    return {
        **captured,
        "answer": result["answer"],
        "model": result["model"],
        "passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(exercise(), ensure_ascii=False, indent=2))
