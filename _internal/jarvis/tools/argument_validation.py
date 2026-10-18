"""Dependency-free JSON-schema subset used by the built-in tool contracts."""
from __future__ import annotations
import math
import re
from typing import Any

# Published legacy spellings are normalised *before* schema/risk evaluation.
ACTION_ALIASES = {
    "filesystem": {"open_file": "open", "open_folder": "open", "launch": "open", "execute": "open", "new_file": "create_file", "edit_text": "write_text", "edit_file": "write_text", "replace": "replace_text", "replace_in_file": "replace_text", "trash": "delete", "recycle": "delete", "delete_file": "delete", "move_file": "move", "rename_file": "rename", "copy_file": "copy"},
    "game_locations": {"query": "find"},
}
for _provider in ("music", "spotify", "yandex_music"):
    ACTION_ALIASES[_provider] = {
        **{name: "play_track" for name in ("play_song", "start_track")},
        "find_track": "search_track", "find_song": "search_track",
        "repeat_on": "repeat_track",
    }
ACTION_ALIASES["spotify"].update({"play_music": "play", "queue_next": "queue_add", "connect": "authorize"})
ACTION_ALIASES["yandex_music"].update({"play_music": "my_wave", "open_playlist": "playlist", "play_playlist": "playlist", "find_playlist": "playlist", "search_playlist": "playlist", "playlist_by_name": "playlist", "start_playlist": "playlist"})


def normalize_tool_call(tool: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    result = normalize_arguments(tool, arguments)
    # Historical callers used media as a transport facade even after local
    # music gained separate providers. Preserve the explicit provider without
    # widening the ownership firewall to unrelated video/browser actions.
    if tool == "media" and result.get("action") in {"play", "pause", "stop", "next", "previous", "status", "play_track"}:
        target = str(result.get("target", "")).casefold()
        if target in {"yandex", "yandex_music", "spotify"}:
            tool = "spotify" if target == "spotify" else "yandex_music"
        elif result.get("action") == "play_track" and target in {"", "music"}:
            tool = "music"
        if tool != "media":
            result.pop("target", None)
            result = normalize_arguments(tool, result)
    return tool, result


def normalize_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = dict(arguments)
    if isinstance(result.get("action"), str):
        action = result["action"].strip().casefold()
        result["action"] = ACTION_ALIASES.get(tool, {}).get(action, action)
    return result


def validate(value: Any, schema: dict[str, Any], path: str = "arguments", depth: int = 0) -> list[str]:
    """Return bounded human-readable errors; no coercion of booleans or strings."""
    if depth > 12:
        return [path + ": nesting limit"]
    if not isinstance(schema, dict):
        return []
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(not validate(value, {**schema, "type": item}, path, depth + 1) for item in expected):
            return [path + ": wrong type"]
        return []
    types = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        "null": value is None,
    }
    if expected in types and not types[expected]:
        return [path + ": expected " + str(expected)]
    if "enum" in schema and value not in schema["enum"]:
        return [path + ": unsupported value"]
    errors = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value or value[name] is None or (isinstance(value[name], str) and not value[name].strip()):
                errors.append(path + "." + name + ": required")
        for key, item in list(value.items())[:256]:
            if key in properties:
                errors.extend(validate(item, properties[key], path + "." + str(key), depth + 1))
            elif schema.get("additionalProperties") is False:
                errors.append(path + "." + str(key) + ": unknown property")
        if len(value) > 256:
            errors.append(path + ": too many properties")
    elif isinstance(value, list):
        if len(value) > int(schema.get("maxItems", 1000)):
            errors.append(path + ": too many items")
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(path + ": not enough items")
        for i, item in enumerate(value[:1000]):
            errors.extend(validate(item, schema.get("items", {}), path + "[" + str(i) + "]", depth + 1))
            if len(errors) >= 12:
                break
    elif isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)) or len(value) > int(schema.get("maxLength", 2_000_000)):
            errors.append(path + ": invalid length")
        if schema.get("pattern") and not re.search(str(schema["pattern"]), value):
            errors.append(path + ": invalid format")
    elif isinstance(value, (float, int)) and not isinstance(value, bool):
        if not math.isfinite(value):
            errors.append(path + ": number must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(path + ": below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(path + ": above maximum")
    return errors[:12]
