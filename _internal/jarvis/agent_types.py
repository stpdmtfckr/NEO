# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.security import Risk


@dataclass
class PendingAction:
    tool_name: str
    arguments: dict[str, Any]
    risk: Risk
    summary: str
    confirmation_id: str = ""


@dataclass
class AgentTurn:
    text: str
    pending: PendingAction | None = None
    tool_details: list[str] | None = None
    success: bool | None = None
    outcome: str = ""


__all__ = ["PendingAction", "AgentTurn"]
