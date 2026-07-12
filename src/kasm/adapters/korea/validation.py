"""Deterministic parser quality reports used by fixtures and live sync dry-runs."""

from __future__ import annotations

from dataclasses import dataclass

from .parser import ParseResult


@dataclass(frozen=True, slots=True)
class GoldenBoundary:
    speaker_name: str
    speaker_role: str | None = None


@dataclass(frozen=True, slots=True)
class ParserQualityReport:
    expected_boundaries: int
    parsed_boundaries: int
    matched_boundaries: int
    duplicate_sequences: int
    empty_speeches: int
    missing_locators: int
    parse_failures: int

    @property
    def precision(self) -> float:
        return self.matched_boundaries / self.parsed_boundaries if self.parsed_boundaries else 0.0

    @property
    def recall(self) -> float:
        if not self.expected_boundaries:
            return 0.0
        return self.matched_boundaries / self.expected_boundaries

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    @property
    def provenance_complete(self) -> bool:
        return self.missing_locators == 0


def evaluate_parse(
    result: ParseResult, expected: list[GoldenBoundary] | tuple[GoldenBoundary, ...]
) -> ParserQualityReport:
    """Compare the ordered speaker boundary sequence with a reviewed golden list."""

    parsed = [(speech.speaker_name, speech.speaker_role) for speech in result.speeches]
    gold = [(boundary.speaker_name, boundary.speaker_role) for boundary in expected]
    matched = sum(actual == wanted for actual, wanted in zip(parsed, gold, strict=False))
    sequences = [speech.sequence for speech in result.speeches]
    return ParserQualityReport(
        expected_boundaries=len(gold),
        parsed_boundaries=len(parsed),
        matched_boundaries=matched,
        duplicate_sequences=len(sequences) - len(set(sequences)),
        empty_speeches=sum(not speech.text.strip() for speech in result.speeches),
        missing_locators=sum(not speech.source_locator for speech in result.speeches),
        parse_failures=len(result.failures),
    )
