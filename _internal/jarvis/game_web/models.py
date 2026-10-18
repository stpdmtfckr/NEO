# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SearchResult:
    """One ranked Google AI source returned through SearchAPI.io."""

    title: str
    url: str
    snippet: str = ""
    provider: str = "searchapi_google_ai_mode"
    rank: int = 0
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SearchResponse:
    """A complete Google AI Mode turn delivered through SearchAPI.io.

    `direct_answer` is Google's AI-generated answer returned by the provider.
    If Google AI does not return an answer, production fails closed instead of
    asking the local model to fill gameplay facts from memory.
    """

    query: str
    provider: str
    results: list[SearchResult] = field(default_factory=list)
    direct_answer: str = ""
    direct_answer_kind: str = "google_ai_mode_answer"
    related_questions: list[str] = field(default_factory=list)
    continuation_token: str = ""
    blocked: bool = False
    error: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Evidence:
    """Compatibility source container for diagnostics/extensions."""

    title: str
    url: str
    text: str
    provider: str = ""
    kind: str = "reference"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
