# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
"""Deterministic routing layers for NEO JARVIS.

The package deliberately separates domain ownership from the legacy compatibility
path. New handlers should live here; the legacy direct-action grammar remains a
fail-safe while domains are migrated one at a time.
"""

from jarvis.routing.domain_dispatcher import DomainDispatcher, DomainDecision

__all__ = ["DomainDispatcher", "DomainDecision"]
