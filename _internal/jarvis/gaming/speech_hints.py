# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Compact STT hints from the process-detected game only.

The process-detected game's display name and aliases are enough to keep proper
names stable without feeding a large vocabulary into Whisper.
"""

from typing import Any

from jarvis.gaming.manager import GameManager
from jarvis.gaming.runtime import active_game_context


def command_game_initial_prompt(config: Any, base_prompt: str = "") -> str:
    try:
        enabled = bool(config.get("gaming.web_knowledge.stt_context_hints", True))
    except Exception:
        enabled = True
    if not enabled:
        return str(base_prompt or "")
    current = active_game_context(max_age_seconds=60.0) or {}
    game_id = str(current.get("game_id") or "").casefold().strip()
    game_name = str(current.get("game_name") or "").strip()
    if not game_id and not game_name:
        return str(base_prompt or "")
    terms: list[str] = []
    if game_name:
        terms.append(game_name)
    try:
        manager = GameManager(enabled=True, config=config)
        adapter = next((a for a in manager.adapters if a.game_id == game_id), None)
        if adapter is not None:
            for value in (adapter.display_name, *adapter.aliases):
                value = str(value or "").strip()
                if value and value.casefold() not in {x.casefold() for x in terms}:
                    terms.append(value)
                if len(terms) >= 16:
                    break
    except Exception:
        pass
    if not terms:
        return str(base_prompt or "")
    extra = "Active game vocabulary: " + ", ".join(terms[:16]) + ". Preserve game proper names exactly; do not translate them."
    base = str(base_prompt or "").strip()
    return f"{base} {extra}".strip()


__all__ = ["command_game_initial_prompt"]
