# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Single ownership dispatcher for deterministic command routing.

The dispatcher does *classification and route ownership only*.  It never executes
an OS action and therefore remains cheap enough to run for every turn.  Execution,
confirmation and conversational state stay in :class:`jarvis.agent.Agent`.

This creates a stable migration seam: mature domain grammars can be moved out of
the legacy direct-action compatibility layer without changing the Agent contract.
"""

from dataclasses import dataclass
import time
from typing import Any, Callable, Iterable

from jarvis.language import language_code
from jarvis.runtime.router import RoutedIntent


@dataclass(frozen=True)
class DomainDecision:
    routed: RoutedIntent | None
    semantic: Any | None
    context: Any | None
    source: str
    elapsed_seconds: float

    @property
    def domain(self) -> str:
        routed = self.routed
        return routed.domain if routed is not None else "conversation"

    @property
    def owner(self) -> str:
        """Concrete execution owner, finer grained than the broad runtime domain."""
        routed = self.routed
        if routed is None:
            return "conversation"
        tool = str(getattr(routed, "tool", "") or "").casefold()
        owner_map = {
            "audio": "audio",
            "game": "gaming",
            "discord": "discord",
            "yandex_music": "media",
            "spotify": "media",
            "music": "media",
            "media": "media",
            "browser": "browser",
            "filesystem": "filesystem",
            "application": "system",
            "window": "system",
            "system": "system",
        }
        return owner_map.get(tool, self.domain)

    @property
    def claimed(self) -> bool:
        return self.routed is not None


class DomainDispatcher:
    """Resolve one utterance to a deterministic owner with no side effects."""

    def __init__(self, config: Any, *, router: Any, atlas2: Any | None = None) -> None:
        self.config = config
        self.router = router
        self.atlas2 = atlas2

    def resolve(
        self,
        text: str,
        *,
        scenario_phrases: Iterable[str] | None = None,
        semantic_text: str = "",
    ) -> DomainDecision:
        started = time.perf_counter()
        source = "legacy.static"
        semantic = None
        context = None
        routed = None
        phrases = list(scenario_phrases or ())

        atlas = self.atlas2
        if atlas is not None and bool(self.config.get("routing.atlas2_enabled", True)):
            resolution = atlas.resolve(
                text,
                router=self.router,
                language=language_code(self.config),
                semantic_text=str(semantic_text or text),
                scenario_phrases=phrases,
            )
            routed = resolution.routed
            semantic = resolution.semantic
            context = resolution.context
            source = resolution.source
            if context is None:
                try:
                    context = atlas.context()
                except Exception:
                    context = resolution.context
        else:
            routed = self.router.route(text, scenario_phrases=phrases)

        return DomainDecision(
            routed=routed,
            semantic=semantic,
            context=context,
            source=source,
            elapsed_seconds=time.perf_counter() - started,
        )


__all__ = ["DomainDispatcher", "DomainDecision"]
