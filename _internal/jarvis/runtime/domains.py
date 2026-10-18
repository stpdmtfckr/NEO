# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Canonical domain classifier for NEO JARVIS commands and tool actions.

HOTFIX89 turns the previously scattered domain hints into one shared taxonomy.
The classifier is deliberately deterministic and cheap: it does not call an LLM
and does not execute tools.  Router, developer diagnostics and context memory use
the same decision so gaming/system/media commands cannot silently drift into
unrelated subsystems over time.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DomainDecision:
    domain: str
    intent: str
    tool: str
    action: str = ""
    transport: str = "local"


_SYSTEM_TOOLS = {
    "system", "hardware", "audio", "application", "window", "filesystem", "desktop",
    "screen", "settings", "process", "clipboard", "archive", "text_edit",
    "diagnostics", "network", "input_control", "search", "developer", "powershell",
    "history", "window_advanced", "transfer", "system_monitor", "capture", "ui", "copy", "knowledge", "plugins", "neo_model", "assistant_model", "task",
}
_BROWSER_TOOLS = {"browser", "browser_advanced", "browser_nav"}
_AUTOMATION_TOOLS = {"automation", "scenario", "timer", "overlay", "google_calendar"}
_COMMUNICATION_TOOLS = {"discord", "email", "telegram_messages"}
_MEDIA_TOOLS = {"media", "music", "yandex_music", "spotify"}


def _action(arguments: dict[str, Any] | None) -> str:
    return str((arguments or {}).get("action", "") or "").strip().casefold()


def classify_domain(
    tool: str,
    *,
    intent: str = "",
    arguments: dict[str, Any] | None = None,
    transport: str = "local",
) -> DomainDecision:
    tool_key = str(tool or "").strip().casefold()
    intent_key = str(intent or "").strip().casefold()
    action = _action(arguments)
    args = arguments or {}

    if tool_key in {"game", "match"} or (
        tool_key == "application" and str(args.get("kind", "") or "").strip().casefold() == "game"
    ) or intent_key.startswith("gaming") or intent_key.startswith("game_"):
        domain = "gaming"
    elif tool_key == "discord" and action.startswith("music_"):
        domain = "media"
    elif tool_key in _MEDIA_TOOLS or intent_key.startswith(("youtube_", "yandex_", "spotify_", "music_", "media_")):
        domain = "media"
    elif tool_key in _BROWSER_TOOLS:
        domain = "browser"
    elif tool_key in _SYSTEM_TOOLS:
        domain = "system"
    elif tool_key in _AUTOMATION_TOOLS:
        domain = "automation"
    elif tool_key in _COMMUNICATION_TOOLS:
        domain = "communication"
    elif tool_key in {"translation", "translate"}:
        domain = "language"
    elif tool_key in {"memory", "briefing", "weather", "date_info"}:
        domain = "assistant"
    else:
        domain = "general"

    # Telegram is a transport, not a duplicate copy of every domain.  Remote-only
    # control-plane intents can still explicitly opt into domain=remote by naming
    # the intent remote.*.
    if intent_key.startswith("remote"):
        domain = "remote"

    return DomainDecision(
        domain=domain,
        intent=intent_key or (f"{tool_key}.{action}" if action else tool_key),
        tool=tool_key,
        action=action,
        transport=str(transport or "local").strip().casefold() or "local",
    )


def domain_allows_tool(domain: str, tool: str, arguments: dict[str, Any] | None = None) -> bool:
    """Conservative family guard used by deterministic routing diagnostics.

    This is not the security policy.  It only catches clear cross-domain mistakes,
    e.g. a typed game routed as a generic browser command.
    """
    d = str(domain or "").strip().casefold()
    decision = classify_domain(tool, arguments=arguments)
    if not d or d in {"general", decision.domain}:
        return True
    compatible = {
        "system": {"system", "automation"},
        "gaming": {"gaming", "system", "automation"},
        "media": {"media", "communication", "automation"},
        "browser": {"browser", "system", "automation"},
        "communication": {"communication", "system", "automation"},
        "remote": {"remote", "system", "gaming", "media", "browser", "communication", "automation"},
    }
    return decision.domain in compatible.get(d, {d})
