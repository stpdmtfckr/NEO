# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class PluginContext:
    """Stable services exposed to built-in and third-party plugins.

    Plugins should depend on this small context rather than MainWindow/Agent
    internals. ``registry`` is the only capability required to add tools; the
    remaining services are explicit dependencies for integrations that need them.
    """

    registry: Any
    config: Any
    database: Any
    ollama: Any
    plugin_name: str = ""


class _Dispatch:
    def execute(self, *args, **kwargs):
        return self._registry.execute(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._registry.get(*args, **kwargs)

    def risk_for(self, *args, **kwargs):
        return self._registry.risk_for(*args, **kwargs)


class RegistryView(_Dispatch):
    """Filtered registry facade kept for compatibility with small registrars."""

    def __init__(self, registry: Any, allowed: Iterable[str]) -> None:
        self._registry = registry
        self._allowed = {str(x) for x in allowed}
        self.config = registry.config
        self.database = registry.database
        self.ollama = registry.ollama
        self.security = registry.security

    def register(self, tool: Any) -> None:
        if str(getattr(tool, "name", "")) in self._allowed:
            self._registry.register(tool)


class PluginDispatchRegistry(_Dispatch):
    """Route one legacy registrar pass into independently owned plugins.

    Older JARVIS modules register several tools from one function. Calling that
    function once through this facade preserves all initialization/order behavior
    while giving every tool an explicit plugin owner and allowing a whole plugin
    to be disabled without copying or rewriting its implementation.
    """

    def __init__(self, registry: Any, owners: Mapping[str, str], disabled: Iterable[str] = ()) -> None:
        self._registry = registry
        self._owners = {str(tool): str(plugin) for tool, plugin in owners.items()}
        self._disabled = {str(x) for x in disabled}
        self.loaded_plugins: set[str] = set()
        self.config = registry.config
        self.database = registry.database
        self.ollama = registry.ollama
        self.security = registry.security

    def register(self, tool: Any) -> None:
        tool_name = str(getattr(tool, "name", ""))
        plugin_name = self._owners.get(tool_name, "core.unclassified")
        if plugin_name in self._disabled:
            return
        self._registry.register(tool)
        self.loaded_plugins.add(plugin_name)
        owners = getattr(self._registry, "tool_plugin_owners", None)
        if not isinstance(owners, dict):
            owners = {}
            setattr(self._registry, "tool_plugin_owners", owners)
        owners[tool_name] = plugin_name
