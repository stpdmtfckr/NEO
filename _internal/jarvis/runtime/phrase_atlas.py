# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Combinatorial phrase atlas and domain-lock helpers.

The atlas does not enumerate every sentence in RAM.  It stores action/object
families and safe templates, giving tens of thousands of equivalent constructions
while preserving arbitrary payloads such as file names, songs and app names.
"""

import json
from jarvis.runtime import cached_re as re
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def atlas() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "config" / "phrase_atlas.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold().replace("ё", "е")).strip(" \t\r\n.,!?;:—-")


def estimated_variation_count() -> int:
    data = atlas()
    actions = sum(max(1, len(v)) for v in (data.get("actions", {}) or {}).values())
    objects = sum(max(1, len(v)) for v in (data.get("objects", {}) or {}).values())
    templates = max(1, len(data.get("templates", []) or []))
    prefixes = max(1, len(data.get("polite_prefixes", []) or []))
    suffixes = max(1, len(data.get("polite_suffixes", []) or []))
    # Conservative combinatorial estimate: not all prefixes/suffixes combine with
    # every template in production, but this shows the atlas' intended coverage.
    return actions * objects * templates * max(1, prefixes // 2) * max(1, suffixes // 2)


@lru_cache(maxsize=1)
def _action_aliases() -> tuple[tuple[str, str], ...]:
    actions = atlas().get("actions", {}) or {}
    canonical_keys = {_norm(name): str(name) for name in actions if _norm(name)}
    # Canonical action words always win over the same spelling reused as a loose
    # synonym by another family (e.g. «покажи» must remain «покажи», not become
    # «открой»). This keeps phrase expansion deterministic and domain-safe.
    chosen: dict[str, str] = dict(canonical_keys)
    for canonical, values in actions.items():
        for value in values or []:
            key = _norm(value)
            if key and key not in chosen:
                chosen[key] = str(canonical)
    return tuple(sorted(chosen.items(), key=lambda item: len(item[0]), reverse=True))


@lru_cache(maxsize=1)
def _object_aliases() -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for domain, values in (atlas().get("objects", {}) or {}).items():
        result[str(domain)] = tuple(sorted({_norm(x) for x in values if _norm(x)}, key=len, reverse=True))
    return result


def _ci_prefix_match(value: str, phrase: str) -> re.Match[str] | None:
    words = [re.escape(part) for part in re.split(r"\s+", str(phrase or "").strip()) if part]
    if not words:
        return None
    return re.match(r"^" + r"\s+".join(words) + r"(?:\s+|$)", value, flags=re.I)


def _strip_polite_prefix(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" \t\r\n.,!?;:—-")
    prefixes = sorted({str(x).strip() for x in (atlas().get("polite_prefixes", []) or []) if str(x).strip()}, key=len, reverse=True)
    changed = True
    while changed and value:
        changed = False
        for prefix in prefixes:
            m = _ci_prefix_match(value, prefix)
            if m and m.end() < len(value) + 1:
                tail = value[m.end():].strip(" ,.-")
                if tail:
                    value = tail
                    changed = True
                    break
    value = re.sub(
        r"^(?:ты\s+)?(?:можешь|можете|сможешь|не\s+мог\s+бы|не\s+могла\s+бы)\s+",
        "", value, flags=re.I,
    )
    return value.strip()


def _strip_polite_suffix(text: str) -> str:
    value = str(text or "").strip()
    suffixes = sorted({str(x).strip() for x in (atlas().get("polite_suffixes", []) or []) if str(x).strip()}, key=len, reverse=True)
    for suffix in suffixes:
        m = re.search(r"(?:\s+|^)" + r"\s+".join(re.escape(p) for p in suffix.split()) + r"$", value, flags=re.I)
        if m and m.start() > 0:
            value = value[:m.start()].rstrip(" ,.-")
            break
    return value


def _canonical_action_prefix(text: str) -> str:
    value = str(text or "").strip()
    for alias, canonical in _action_aliases():
        parts = [re.escape(part) for part in alias.split()]
        m = re.match(r"^" + r"\s+".join(parts) + r"(?:\s+|$)", value, flags=re.I)
        if m:
            tail = value[m.end():]
            return canonical + (" " + tail if tail else "")
    return value


def _canonical_object_after_action(text: str) -> str:
    """Normalize only an explicit object marker immediately after the action."""
    value = str(text or "").strip()
    first_match = re.match(r"^(\S+)(?:\s+)(.+)$", value, flags=re.S)
    if not first_match:
        return value
    first, rest = first_match.group(1), first_match.group(2)
    for domain, aliases in _object_aliases().items():
        for alias in aliases:
            parts = [re.escape(part) for part in alias.split()]
            m = re.match(r"^" + r"\s+".join(parts) + r"(?:\s+|$)", rest, flags=re.I)
            if not m:
                continue
            canonical = {
                "folder": "папку", "file": "файл", "application": "приложение",
                "window": "окно", "overlay": "панель", "notes": "заметки",
                "checklist": "чек-лист", "browser": "браузер", "tab": "вкладку",
                "yandex_music": "яндекс музыку", "discord": "discord",
                "youtube": "youtube", "monitor": "экран",
                "process": "процесс", "settings": "настройки", "game": "игру",
                "audio": "звук", "microphone": "микрофон", "clipboard": "буфер обмена",
                "archive": "архив", "screenshot": "скриншот", "translation": "перевод",
            }.get(domain, alias)
            tail = rest[m.end():]
            return f"{first} {canonical}" + (f" {tail}" if tail else "")
    return value


def _reorder_object_first(text: str) -> str:
    """Support safe object-first speech such as ``папку Downloads открой``."""
    value = str(text or "").strip()
    action_aliases = _action_aliases()
    for domain in ("folder", "file", "application", "window", "overlay", "notes", "checklist", "tab", "process", "settings", "game", "audio", "microphone", "clipboard", "archive", "screenshot"):
        for obj_alias in _object_aliases().get(domain, ()):
            obj_parts = [re.escape(part) for part in obj_alias.split()]
            obj_match = re.match(r"^" + r"\s+".join(obj_parts) + r"\s+(.+)$", value, flags=re.I | re.S)
            if not obj_match:
                continue
            body = obj_match.group(1).strip()
            for action_alias, canonical in action_aliases:
                action_parts = [re.escape(part) for part in action_alias.split()]
                action_match = re.search(r"\s+" + r"\s+".join(action_parts) + r"$", body, flags=re.I)
                if not action_match:
                    continue
                target = body[:action_match.start()].strip()
                if not target:
                    continue
                canonical_obj = {
                    "folder": "папку", "file": "файл", "application": "приложение",
                    "window": "окно", "overlay": "панель", "notes": "заметки",
                    "checklist": "чек-лист", "tab": "вкладку",
                    "process": "процесс", "settings": "настройки", "game": "игру",
                    "audio": "звук", "microphone": "микрофон", "clipboard": "буфер обмена",
                    "archive": "архив", "screenshot": "скриншот",
                }.get(domain, obj_alias)
                return f"{canonical} {canonical_obj} {target}"
    return value

def normalize_phrase_atlas(text: str) -> str:
    """Normalize natural scaffolding while preserving the user's target payload."""
    original = str(text or "").strip()
    if not original:
        return original
    value = _strip_polite_prefix(original)
    value = _strip_polite_suffix(value)
    value = _reorder_object_first(value)
    value = _canonical_action_prefix(value)
    value = _canonical_object_after_action(value)
    return re.sub(r"\s+", " ", value).strip()


def detect_strong_domain(text: str) -> str:
    t = _norm(text)
    if not t:
        return ""
    # Order matters: notes/checklists are JARVIS overlays, not generic files.
    order = (
        "notes", "checklist", "overlay", "discord", "yandex_music", "youtube",
        "folder", "file", "window", "tab", "browser", "application", "process",
        "settings", "game", "microphone", "audio", "clipboard", "archive",
        "screenshot", "translation", "monitor",
    )
    aliases = _object_aliases()
    for domain in order:
        for alias in aliases.get(domain, ()):
            if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", t):
                # Explicit "файл заметок" / "документ заметок" is a real file.
                if domain in {"notes", "checklist", "overlay"} and re.search(r"\b(?:файл|документ)\w*\b", t):
                    return "file"
                return domain
    return ""


def domain_conflict(tool: str, action: str, text: str) -> str:
    """Return a hard phrase-atlas domain conflict, or an empty string."""
    domain = detect_strong_domain(text)
    if not domain:
        return ""
    tool_key = _norm(tool)
    action_key = _norm(action)
    locks = (atlas().get("domain_locks", {}) or {}).get(domain, []) or []
    if tool_key in {_norm(x) for x in locks}:
        return f"phrase atlas: explicit {domain} object cannot be routed to {tool_key}"
    if domain == "window" and tool_key == "application" and action_key in {"open", "launch", "run"} and re.search(r"\b(?:перемест|перенес|передвин|перетащ|сверн|разверн|закро)\w*\b", _norm(text)):
        return "phrase atlas: an existing-window action cannot launch a new application"
    if domain == "folder" and tool_key == "application":
        return "phrase atlas: folder target is filesystem/desktop, not an application"
    if domain in {"notes", "checklist", "overlay"} and tool_key == "filesystem" and not re.search(r"\b(?:файл|документ)\w*\b", _norm(text)):
        return "phrase atlas: NEO panel target is overlay, not filesystem"
    return ""
