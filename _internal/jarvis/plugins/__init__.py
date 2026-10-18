# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
"""NEO JARVIS plugin API.

Built-in capabilities and user plugins use the same small registration contract.
The compatibility loader in :mod:`jarvis.runtime.plugins` still accepts the old
``register(registry)`` entry point, so existing local plugins do not break.
"""
from .api import PluginContext, RegistryView

__all__ = ["PluginContext", "RegistryView"]
