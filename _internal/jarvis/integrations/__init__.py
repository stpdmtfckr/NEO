# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
"""Application-specific integrations for NEO JARVIS.

These adapters sit above the generic Windows automation layer.  They expose a
small deterministic vocabulary to Agent while keeping application-specific UI
and API fallback logic out of the main router.
"""

from .intents import ParsedIntegrationIntent, parse_integration_intent

__all__ = ["ParsedIntegrationIntent", "parse_integration_intent"]
