"""NEO 169: stage 3 of the request cascade — the NEO command model.

Filled in by the model runtime (see :mod:`jarvis.neo_model`). The stage returns a
structured proposal (registered tool, arguments, clarification flag); the agent's
normal executor then checks the tool, arguments, permissions and confirmations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Proposal:
    intent: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    needs_clarification: bool = False
    question: str = ''


def propose(agent: Any, text: str, code: str) -> Proposal | None:
    try:
        from jarvis.neo_model import runtime
    except ImportError:
        return None
    return runtime.propose(agent, text, code)
