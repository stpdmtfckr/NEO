# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Read-only runtime health report for NEO JARVIS.

The doctor intentionally does not click UI, alter files, or start games.  It gives a
single report that is useful when a Windows-specific failure cannot be reproduced in
the development environment.
"""

import importlib.util
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from jarvis.config import ConfigManager
from jarvis.gaming import GameManager, recent_game_launch
from jarvis.gaming.history import GameSessionHistoryStore
from jarvis.ollama_runtime import find_ollama_executable, probe_ollama
from jarvis.music_app_control import find_yandex_music_install
from jarvis.integrations.discord import _discord_processes
from jarvis.secrets import yandex_music_token


def _module(name: str) -> dict[str, Any]:
    try:
        spec = importlib.util.find_spec(name)
        return {"available": spec is not None}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _monitors() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    try:
        from jarvis.display_mapping import windows_monitor_geometries
        return [
            {"windows_number": number, "geometry": list(rect), "device": device}
            for number, rect, device in windows_monitor_geometries()
        ]
    except Exception as exc:
        return [{"error": str(exc)}]


def collect(root: Path) -> dict[str, Any]:
    config = ConfigManager(root / "config" / "default_settings.json")
    base_url = str(config.get("ollama.base_url", "http://localhost:11434") or "http://localhost:11434")
    ollama_ready, ollama_version = probe_ollama(base_url, timeout=1.0)
    manager = GameManager(enabled=bool(config.get("gaming.enabled", True)), config=config)
    try:
        game = manager.snapshot()
        game_snapshot = game.as_dict() if game is not None else None
    except Exception as exc:
        game_snapshot = {"error": str(exc)}
    overlays_path = config.paths.root / "overlays.json"
    overlay_summary: dict[str, Any] = {"path": str(overlays_path), "exists": overlays_path.exists()}
    if overlays_path.exists():
        try:
            payload = json.loads(overlays_path.read_text(encoding="utf-8"))
            panels = payload.get("panels", {}) if isinstance(payload, dict) else {}
            overlay_summary["panels"] = []
            for name, value in (panels.items() if isinstance(panels, dict) else []):
                if not isinstance(value, dict):
                    continue
                binding = value.get("binding") if isinstance(value.get("binding"), dict) else {}
                overlay_summary["panels"].append({
                    "name": str(name),
                    "kind": str(value.get("kind", "")),
                    "purpose": str(value.get("purpose", "") or ""),
                    "game_id": str(value.get("game_id", "") or ""),
                    "game_name": str(value.get("game_name", "") or ""),
                    "aliases": list(value.get("aliases", []) or [])[:12],
                    "visible": bool(value.get("visible", False)),
                    "monitor": value.get("monitor"),
                    "position": value.get("position"),
                    "binding": {
                        "type": binding.get("type"), "status": binding.get("status"),
                        "pid": binding.get("pid"), "session_started_at": binding.get("session_started_at"),
                        "last_telemetry_at": binding.get("last_telemetry_at"),
                    } if binding else None,
                    "updated_at": value.get("updated_at"),
                })
        except Exception as exc:
            overlay_summary["error"] = str(exc)

    history_path = config.paths.root / "game_sessions.json"
    history = GameSessionHistoryStore(history_path)
    try:
        history_summary = history.summary_all()
        history_latest = history.all_sessions()[:12]
    except Exception as exc:
        history_summary = {"error": {"message": str(exc)}}
        history_latest = []

    exe = find_ollama_executable()
    try:
        yandex_install = find_yandex_music_install() if os.name == "nt" else None
    except Exception as exc:
        yandex_install = {"error": str(exc)}
    try:
        discord_running = bool(_discord_processes()) if os.name == "nt" else False
    except Exception:
        discord_running = False
    integrations = {
        "yandex_music": {
            "enabled": bool(config.get("integrations.yandex_music.enabled", True)),
            "api_enabled": bool(config.get("integrations.yandex_music.api_enabled", True)),
            "api_token_configured": bool(yandex_music_token(config)),
            "ui_fallback": bool(config.get("integrations.yandex_music.ui_fallback", True)),
            "native_install": yandex_install,
            "python_library": _module("yandex_music"),
        },
        "discord": {
            "enabled": bool(config.get("integrations.discord.enabled", True)),
            "running": discord_running,
            "default_server": str(config.get("integrations.discord.default_server", "") or ""),
            "music_text_channel": str(config.get("integrations.discord.music_text_channel", "") or ""),
            "default_voice_channel": str(config.get("integrations.discord.default_voice_channel", "") or ""),
            "music_bot_name": str(config.get("integrations.discord.music_bot_name", "") or ""),
            "music_bot_mode": str(config.get("integrations.discord.music_bot_mode", "slash") or "slash"),
            "music_query_option": str(config.get("integrations.discord.music_query_option", "query") or "query"),
            "user_token_stored": False,
        },
    }
    return {
        "version": (root / "VERSION.txt").read_text(encoding="utf-8").strip() if (root / "VERSION.txt").exists() else "unknown",
        "platform": {
            "system": platform.system(), "release": platform.release(), "version": platform.version(),
            "python": sys.version.split()[0], "executable": sys.executable, "windows": os.name == "nt",
        },
        "modules": {name: _module(name) for name in (
            "PySide6", "sounddevice", "faster_whisper", "pywinauto", "psutil", "winsdk", "mss", "PIL", "requests", "yandex_music"
        )},
        "ollama": {
            "base_url": base_url, "ready": ollama_ready, "version": ollama_version,
            "executable": str(exe) if exe else "", "auto_start": bool(config.get("ollama.auto_start", True)),
        },
        "display": {"monitors": _monitors()},
        "integrations": integrations,
        "gaming": {
            "enabled": manager.enabled, "process_only": True,
            "current": game_snapshot, "recent_launch": recent_game_launch(max_age_seconds=1200),
            "supported": manager.supported_games(),
            "history_path": str(history_path), "history_summary": history_summary,
            "history_latest": history_latest,
        },
        "overlays": overlay_summary,
        "paths": {
            "data": str(config.paths.root), "logs": str(config.paths.logs), "backups": str(config.paths.backups),
        },
    }


def render(report: dict[str, Any]) -> str:
    p = report["platform"]
    lines = [
        "=== NEO DOCTOR ===",
        f"Version: {report['version']}",
        f"OS: {p['system']} {p['release']} | Python {p['python']}",
        "",
        "Dependencies:",
    ]
    for name, status in report["modules"].items():
        lines.append(f"  {'OK ' if status.get('available') else 'MISS'} {name}")
    o = report["ollama"]
    lines.extend([
        "", "Ollama:",
        f"  ready={o['ready']} version={o['version'] or '-'} auto_start={o['auto_start']}",
        f"  executable={o['executable'] or 'NOT FOUND'}",
        f"  api={o['base_url']}",
        "", "Monitors:",
    ])
    monitors = report["display"]["monitors"]
    if not monitors:
        lines.append("  unavailable outside Windows")
    else:
        for item in monitors:
            if item.get("error"):
                lines.append(f"  ERROR {item['error']}")
            else:
                lines.append(f"  DISPLAY{item['windows_number']} {item['geometry']} {item['device']}")
    integrations = report.get("integrations", {})
    ym = integrations.get("yandex_music", {})
    dc = integrations.get("discord", {})
    lines.extend([
        "", "Application integrations:",
        f"  Yandex Music: enabled={ym.get('enabled')} api={ym.get('api_enabled')} "
        f"token={'configured' if ym.get('api_token_configured') else 'not configured'} "
        f"ui_fallback={ym.get('ui_fallback')}",
        f"  Discord: enabled={dc.get('enabled')} running={dc.get('running')} "
        f"server={dc.get('default_server') or '-'} music_channel={dc.get('music_text_channel') or '-'} "
        f"voice={dc.get('default_voice_channel') or '-'} bot={dc.get('music_bot_name') or '-'}",
        "  Discord user-token/self-bot: disabled by design",
    ])
    g = report["gaming"]
    lines.extend(["", "Gaming:", f"  current={json.dumps(g['current'], ensure_ascii=False, default=str) if g['current'] else 'none'}"])
    if g.get("recent_launch"):
        lines.append("  recent_launch=" + json.dumps(g["recent_launch"], ensure_ascii=False, default=str))
    lines.append(f"  history={g.get('history_path', '-')}")
    summaries = g.get("history_summary", {})
    if isinstance(summaries, dict):
        for gid, summary in summaries.items():
            if isinstance(summary, dict):
                lines.append(
                    f"  history[{gid}] sessions={summary.get('sessions', 0)} "
                    f"running={summary.get('running_sessions', 0)} total_seconds={summary.get('total_seconds', 0)}"
                )
    lines.extend(["", "Overlays:"])
    panels = report["overlays"].get("panels", [])
    if not panels:
        lines.append("  none")
    else:
        for panel in panels:
            binding = panel.get("binding") or {}
            bind_text = ""
            if isinstance(binding, dict) and binding:
                bind_text = (
                    f" | bind={binding.get('type')}:{binding.get('status')} "
                    f"pid={binding.get('pid')} telemetry={binding.get('last_telemetry_at')}"
                )
            lines.append(
                f"  {panel['name']} | {panel['kind']} | purpose={panel.get('purpose') or '-'} "
                f"| game={panel.get('game_id') or '-'} | visible={panel['visible']} "
                f"| monitor={panel['monitor']} | {panel['position']}{bind_text}"
            )
    if report["overlays"].get("error"):
        lines.append("  ERROR " + str(report["overlays"]["error"]))
    lines.extend(["", "Data paths:", f"  {report['paths']['data']}", f"  logs: {report['paths']['logs']}"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent.parent
    report = collect(root)
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
