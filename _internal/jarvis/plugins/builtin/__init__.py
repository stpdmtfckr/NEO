# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.plugins.api import PluginContext, PluginDispatchRegistry


@dataclass(frozen=True)
class BuiltinPlugin:
    name: str
    description: str
    tools: tuple[str, ...]
    source: str


BUILTIN_PLUGINS: tuple[BuiltinPlugin, ...] = (
    BuiltinPlugin("core.files", "Filesystem operations", ("filesystem",), "core"),
    BuiltinPlugin("core.applications", "Applications, desktop and window control", ("application", "desktop", "window", "input_control"), "core"),
    BuiltinPlugin("ui.panels", "NEO panels and overlays", ("overlay",), "core"),
    BuiltinPlugin("gaming.runtime", "Game detection and game-session features", ("game",), "core"),
    BuiltinPlugin("integrations.browser", "Browser control", ("browser",), "core"),
    BuiltinPlugin("integrations.yandex_music", "Yandex Music", ("yandex_music",), "core"),
    BuiltinPlugin("integrations.discord", "Discord voice/music/navigation", ("discord",), "core"),
    BuiltinPlugin("integrations.media", "Media and audio transport", ("media", "audio", "music", "spotify"), "core"),
    BuiltinPlugin("system.desktop", "System, screenshots and network", ("system", "screen", "network"), "core"),
    BuiltinPlugin("memory.local", "Local assistant memory", ("memory",), "core"),
    BuiltinPlugin("developer.tools", "Developer and PowerShell tools", ("developer", "powershell"), "core"),
    BuiltinPlugin("desktop.input", "Input and assistant control", ("autoclicker", "jarvis"), "core"),
    BuiltinPlugin("desktop.services", "Shelf, downloads and desktop services", ("shelf", "downloads", "activity", "game_locations", "mini_browser", "desktop_features"), "desktop"),

    BuiltinPlugin("automation.local", "Automation and scenarios", ("automation", "scenario"), "extended"),
    BuiltinPlugin("system.hardware", "Hardware and diagnostics", ("hardware", "diagnostics"), "extended"),
    BuiltinPlugin("web.search", "Web/search and advanced browser helpers", ("search", "browser_advanced"), "extended"),
    BuiltinPlugin("clipboard.editing", "Clipboard and text editing", ("clipboard", "text_edit"), "extended"),
    BuiltinPlugin("email.local", "Configured mail provider", ("email",), "extended"),
    BuiltinPlugin("history.local", "Local history", ("history",), "extended"),
    BuiltinPlugin("windows.advanced", "Advanced window control", ("window_advanced",), "extended"),
    BuiltinPlugin("transfer.local", "Local transfer helpers", ("transfer",), "extended"),

    BuiltinPlugin("integrations.weather", "OpenWeather queries", ("weather",), "integrations"),
    BuiltinPlugin("productivity.date", "Date and holiday questions", ("date_info",), "integrations"),
    BuiltinPlugin("utility.chance", "Dice and coin toss", ("chance",), "integrations"),
    BuiltinPlugin("productivity.timer", "Timers and reminders", ("timer",), "integrations"),
    BuiltinPlugin("system.telemetry", "Read-only PC telemetry", ("system_monitor",), "integrations"),
    BuiltinPlugin("productivity.briefing", "Daily briefing", ("briefing",), "integrations"),
    BuiltinPlugin("integrations.google_calendar", "Google Calendar", ("google_calendar",), "integrations"),
    BuiltinPlugin("integrations.telegram_messages", "Telegram Secretary messages", ("telegram_messages",), "integrations"),
    BuiltinPlugin("capture.nvidia", "NVIDIA clips and screen recording", ("capture",), "integrations"),
    BuiltinPlugin("integrations.obs", "Authenticated local OBS Studio control", ("obs_control",), "integrations"),
    BuiltinPlugin("gaming.match_ready", "Match-found notices and accepting a match", ("match",), "integrations"),
    BuiltinPlugin("computer.control", "On-screen control, browser navigation, copy & paste", ("ui", "browser_nav", "copy"), "integrations"),
    BuiltinPlugin("knowledge.local", "Answers from files in folders the user allowed", ("knowledge",), "integrations"),
    BuiltinPlugin("plugins.manager", "User plugin management", ("plugins",), "integrations"),
    BuiltinPlugin("neo.command_model", "NEO command model status, retraining and rollback", ("neo_model",), "integrations"),
    BuiltinPlugin("neo.assistant_model", "NEO assistant model status, install, fine-tuning and rollback", ("assistant_model",), "integrations"),
    BuiltinPlugin("integrations.translation", "Natural text translation", ("translate",), "translation"),
)


def _owners_for(source: str) -> dict[str, str]:
    owners: dict[str, str] = {}
    for plugin in BUILTIN_PLUGINS:
        if plugin.source != source:
            continue
        for tool in plugin.tools:
            owners[tool] = plugin.name
    return owners


def register_all(ctx: PluginContext) -> list[str]:
    """Register built-ins through explicit plugin ownership boundaries.

    Legacy registrar functions are intentionally reused so the refactor changes
    architecture, not behavior. Each registrar runs once through a dispatching
    facade, avoiding duplicate adapter initialization and minimizing regression
    risk in a mature desktop project.
    """
    disabled = set(ctx.config.get("plugins.disabled_builtin", []) or [])
    loaded: set[str] = set()

    from jarvis.tools import implementations
    core_router = PluginDispatchRegistry(ctx.registry, _owners_for("core"), disabled)
    implementations.register_all(core_router)
    loaded.update(core_router.loaded_plugins)

    from jarvis.tools.extended import register_extended
    extended_router = PluginDispatchRegistry(ctx.registry, _owners_for("extended"), disabled)
    register_extended(extended_router)
    loaded.update(extended_router.loaded_plugins)

    from jarvis.tools.integrations import register_integrations
    integrations_router = PluginDispatchRegistry(ctx.registry, _owners_for("integrations"), disabled)
    register_integrations(integrations_router)
    loaded.update(integrations_router.loaded_plugins)

    if "integrations.translation" not in disabled:
        from jarvis.integrations.local_translation import register_translation_tool
        register_translation_tool(ctx.registry)
        ctx.registry.tool_plugin_owners["translate"] = "integrations.translation"
        loaded.add("integrations.translation")

    if "desktop.services" not in disabled:
        from jarvis.tools.desktop_features import register_desktop_features
        desktop_router=PluginDispatchRegistry(ctx.registry,_owners_for('desktop'),disabled)
        register_desktop_features(desktop_router)
        loaded.update(desktop_router.loaded_plugins)

    # Return in catalog order so diagnostics/settings remain stable between runs.
    return [plugin.name for plugin in BUILTIN_PLUGINS if plugin.name in loaded]
