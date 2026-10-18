"""Selected-language, literal command templates with schema-checked slots.

This is an executable grammar, not semantic similarity: the entire command must
match a reviewed template. Payloads are copied, never translated or case-folded.
No model, operating-system operation or network access occurs while parsing.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Callable

from jarvis.runtime.natural_commands import LANGUAGES, negated_action, strip_request_shell
from jarvis.tools.argument_validation import normalize_tool_call, validate

PACK_ROOT = Path(__file__).resolve().parents[2] / "config" / "canonical_phrases"
VARIANT_ROOT = PACK_ROOT.parent / "command_variants"
_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")

# Public spoken slot labels can differ from a mature tool's stable API. These
# mappings live at the grammar boundary; tool implementations remain unchanged.
_SLOT_NAMES = {
    "autoclicker.set_rate": {"value": "cps"},
    "browser.open_url": {"url": "value"}, "browser.open": {"name": "browser"},
    "browser.search": {"query": "value"}, "browser.search_first": {"query": "value"},
    "browser.activate_tab_title": {"title": "value"},
    "browser_advanced.activate_by_topic": {"topic": "query"},
    "browser_advanced.group_by_topic": {"topic": "query"},
    "clipboard.paste_history": {"index": "offset"}, "clipboard.copy_history": {"index": "offset"},
    "developer.run_python_file": {"path": "file_path"},
    "filesystem.write_text": {"text": "content"}, "filesystem.replace_text": {"new_text": "content"},
    "filesystem.create_text_file": {"text": "content"}, "filesystem.append_text": {"text": "content"},
    "filesystem.insert_text": {"text": "content"},
    "system.terminate_process": {"name": "process_name"}, "system.set_power_plan": {"name": "power_plan"},
    "overlay.checklist_check": {"index": "item"}, "overlay.checklist_uncheck": {"index": "item"},
    "overlay.checklist_remove": {"index": "item"}, "overlay.checklist_add": {"text": "item"},
    "window_advanced.list_instances": {"application": "query"},
    "window_advanced.focus_instance": {"application": "query"},
    "window_advanced.move_instance": {"application": "query"},
}
_FIXED = {
    "audio.change_target_volume": {"value": -15}, "audio.mute": {"muted": True},
    "audio.set_input_mute": {"muted": True}, "audio.mute_target": {"muted": True},
    "audio.set_default": {"device_type": "output"},
    "system.set_bluetooth": {"enabled": True}, "system.set_wifi": {"enabled": True},
    "shelf.create": {"kind": "note"}, "shelf.check": {"checked": True},
    "shelf.pin": {"pinned": True}, "mini_browser.pin": {"pinned": True},
}
_INCOMPLETE = {"automation.queue", "automation.parallel"}
_COMPOUND = {
    "ru": r"\s+(?:и|затем|потом)\s+(?:открой|закрой|запусти|удали|создай|выключи|перезагрузи)\b",
    "en": r"\s+(?:and|then)\s+(?:open|close|launch|delete|create|restart|shutdown|shut)\b",
    "de": r"\s+(?:und|dann)\s+(?:öffne|schließe|starte|lösche|erstelle|beende)\b",
    "fr": r"\s+(?:et|puis)\s+(?:ouvre|ferme|lance|supprime|crée|redémarre)\b",
    "es": r"\s+(?:y|luego)\s+(?:abre|cierra|inicia|elimina|crea|reinicia)\b",
    "it": r"\s+(?:e|poi)\s+(?:apri|chiudi|avvia|elimina|crea|riavvia)\b",
    "pt": r"\s+(?:e|depois)\s+(?:abre|fecha|inicia|apaga|cria|reinicia)\b",
}


@dataclass(frozen=True)
class TemplateMatch:
    tool: str = ""
    arguments: dict[str, Any] | None = None
    intent: str = ""
    error: str = ""
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Template:
    intent: str
    phrase: str
    pattern: re.Pattern
    specificity: int


def _literal(value: str) -> str:
    # German uppercase spelling expands ß to SS; re.I alone does not. Only
    # grammar literals get this equivalence. Captured names/text stay unchanged.
    return "".join(r"\s+" if part.isspace() else re.escape(part).replace("’", "'").replace("'", "['’]").replace("règle", "r[èé]gle").replace("ß", "(?:ß|ss)") for part in re.split(r"(\s+)", value))


def _compile(intent: str, phrase: str) -> _Template:
    parts, seen, cursor = [], set(), 0
    for slot in _SLOT.finditer(phrase):
        literal = phrase[cursor:slot.start()]
        parts.append(_literal(literal))
        key = slot.group(1)
        parts.append(r"(?P=" + key + ")" if key in seen else r"(?P<" + key + r">.+?)")
        seen.add(key)
        cursor = slot.end()
    tail = phrase[cursor:]
    parts.append(_literal(tail))
    return _Template(intent, phrase, re.compile("".join(parts), re.I | re.S), len(_SLOT.sub("", phrase).strip()))


# NEO 169: phrase packs of approved user plugins (same 7-language row format as PACK_ROOT).
EXTRA_PACKS: list[Path] = []


# HIST-024: every index derived from the live phrase catalogue follows its version.
CATALOG_VERSION = 0
_DERIVED_CACHES = (
    ("jarvis.runtime.command_repair161", ("catalog", "candidates", "_candidate_heads")),
    ("jarvis.runtime.command_typos165", ("vocabulary", "_index", "word_options", "_tables", "_names")),
    ("jarvis.runtime.multilingual_commands", ("_real_word_cached",)),
    ("jarvis.runtime.clarification_contracts156", ("index", "_head_owners")),
)


def _clear_caches() -> None:
    global CATALOG_VERSION
    CATALOG_VERSION += 1
    for function in (templates, _index, _fast_index):
        function.cache_clear()
    import sys
    for module_name, names in _DERIVED_CACHES:
        module = sys.modules.get(module_name)      # a module never loaded holds no derived state
        if module is None:
            continue
        for name in names:
            clear = getattr(getattr(module, name, None), "cache_clear", None)
            if callable(clear):
                clear()


def register_extra_pack(path: Path) -> None:
    path = Path(path)
    if path not in EXTRA_PACKS:
        EXTRA_PACKS.append(path)
    _clear_caches()


def unregister_extra_pack(path: Path) -> None:
    if Path(path) in EXTRA_PACKS:
        EXTRA_PACKS.remove(Path(path))
    _clear_caches()


@lru_cache(maxsize=7)
def templates(code: str) -> tuple[_Template, ...]:
    if code not in LANGUAGES:
        return ()
    result = []
    seen = set()
    for path in sorted(PACK_ROOT.glob("*.json")) + [p for p in EXTRA_PACKS if p.exists()]:
        for intent, rows in json.loads(path.read_text(encoding="utf-8")).items():
            phrase = strip_request_shell(rows[LANGUAGES.index(code)], code)
            # A payload-only template would give arbitrary text execution power.
            if not _SLOT.sub("", phrase).strip() or phrase.startswith("{"):
                continue
            result.append(_compile(intent, phrase))
            seen.add((intent, phrase.casefold()))
    for path in sorted(VARIANT_ROOT.glob("*.json")):
        for intent, languages in json.loads(path.read_text(encoding="utf-8")).items():
            for phrase in languages.get(code, []):
                phrase = strip_request_shell(phrase, code)
                key = (intent, phrase.casefold())
                if key not in seen and _SLOT.sub("", phrase).strip() and not phrase.startswith("{"):
                    result.append(_compile(intent, phrase))
                    seen.add(key)
    return tuple(result)


@lru_cache(maxsize=7)
def _index(code: str) -> dict[str, tuple[_Template, ...]]:
    grouped: dict[str, list[_Template]] = {}
    for row in templates(code):
        grouped.setdefault(row.phrase.split()[0].casefold(), []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _matching_templates(raw: str, code: str):
    # NEO 183: the full text is matched first, so text/content/body slots keep their «!», «?» or
    # «…»; only name-like slots drop the closing punctuation of the request (see _typed).
    if not raw:
        return []
    exact, prefixes = _fast_index(code)
    exact_rows = exact.get(_index_text(raw), ())
    if exact_rows:
        return [(row, match) for row in exact_rows if (match := row.pattern.fullmatch(raw) or row.pattern.fullmatch(raw.rstrip(" .!?…")))]
    words = _index_text(raw).split()
    rows = []
    for length in range(min(4, len(words)), 0, -1):
        rows.extend(prefixes.get(" ".join(words[:length]), ()))
    return [(row, match) for row in rows if (match := row.pattern.fullmatch(raw) or row.pattern.fullmatch(raw.rstrip(" .!?…")))]


def _index_text(text: str) -> str:
    return " ".join(text.casefold().replace("’", "'").replace("régle", "règle").strip(".!?").split())


@lru_cache(maxsize=7)
def _fast_index(code: str):
    exact, prefixes = {}, {}
    for row in templates(code):
        slot = _SLOT.search(row.phrase)
        if slot is None:
            exact.setdefault(_index_text(row.phrase), []).append(row)
        else:
            # Only complete literal words before the first slot are indexed.
            literal = row.phrase[:slot.start()]
            words = _index_text(literal).split()
            if not literal.endswith(" ") and words:
                words.pop()
            prefix = " ".join(words[:4])
            if prefix:
                prefixes.setdefault(prefix, []).append(row)
            else:
                prefixes.setdefault(_index_text(row.phrase).split()[0], []).append(row)
    return exact, prefixes


def matches_selected_grammar(text: str, code: str) -> bool:
    """Locale evidence only: no schema binding, guessing, or tool execution."""
    raw = str(text or "").strip().lstrip("¿¡")
    return bool(_matching_templates(raw, code) or _matching_templates(strip_request_shell(raw, code), code))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and (value[0], value[-1]) in {(chr(34), chr(34)), ("'", "'"), ("«", "»"), ("“", "”")}:
        return value[1:-1]
    return value


_NAME_SLOTS183 = frozenset({"name", "app", "application", "program", "process", "window", "game", "target", "city", "country"})


def _typed(value: str, schema: dict, key: str, code: str) -> Any:
    quoted = _unquote(value) != value.strip()
    value = _unquote(value)
    if key in _NAME_SLOTS183 and not quoted:
        value = value.rstrip(" ?!.…")          # «…open application Calculator?» names «Calculator»
    kind = schema.get("type", "string")
    if kind in {"integer", "number"} and not re.fullmatch(r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)", value):
        from jarvis.runtime.spoken_numbers import parse_spoken_number
        spoken = parse_spoken_number(value, code)
        if spoken is not None:
            value = str(spoken)
    if kind == "integer":
        if not re.fullmatch(r"[+-]?\d+", value):
            raise ValueError("integer required")
        return int(value)
    if kind == "number":
        if not re.fullmatch(r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)", value):
            raise ValueError("number required")
        return float(value.replace(",", "."))
    if kind == "boolean":
        values = {"true": True, "false": False}
        if value.casefold() not in values:
            raise ValueError("boolean required")
        return values[value.casefold()]
    if kind == "array" and key == "keys":
        # Hotkeys have an explicit, unambiguous separator; arbitrary arrays and
        # objects are never inferred from prose or executed as JSON tool calls.
        keys = [part.strip() for part in value.split("+")]
        if not all(keys):
            raise ValueError("empty hotkey")
        return keys
    if kind in {"array", "object"}:
        raise ValueError("structured input required")
    if schema.get("enum") and value not in schema["enum"]:
        enum_aliases = {
            # NEO 188 atlas: Russian case forms («выбери кнопку … левую») are the same value.
            "left": ("левая", "левую", "левой", "левый", "links", "linke", "gauche", "izquierdo", "izquierda", "sinistro", "sinistra", "esquerdo", "esquerda"),
            "right": ("правая", "правую", "правой", "правый", "rechts", "rechte", "droite", "derecho", "derecha", "destro", "destra", "direito", "direita"),
            "middle": ("средняя", "среднюю", "средней", "средний", "mitte", "mittlere", "milieu", "central", "centrale", "meio"),
        }
        value = next((canonical for canonical, aliases in enum_aliases.items() if value.casefold() in aliases and canonical in schema["enum"]), value)
    return value


_MODE_WORD186 = re.compile(
    r"(?:режим\s+)?(?:перемешиван\w*|перемешк\w*|шаффл|shuffle|повтор\w*|репит|repeat|zufallswiedergabe|"
    r"lecture aléatoire|modo aleatorio|riproduzione casuale|modo aleatório)(?:\s+(?:трек\w*|песн\w*|плейлист\w*|playlist|track|song))?",
    re.I,
)


def _bind(row: _Template, captures: dict[str, str], schema: dict, raw: str, code: str) -> tuple[str, dict]:
    tool, action = row.intent.split(".", 1)
    properties = schema.get("properties", {})
    args = {"action": action} if "action" in properties else {}
    if tool == "media":
        args["target"] = "youtube"
    if row.intent == "discord.music_youtube_url":
        args["play_now"] = True
    for key, value in _FIXED.get(row.intent, {}).items():
        if key in properties:
            args[key] = value
    if row.intent in _INCOMPLETE:
        raise ValueError("tasks required")
    for original_key, value in captures.items():
        key = _SLOT_NAMES.get(row.intent, {}).get(original_key, original_key)
        if key not in properties:
            raise ValueError("unknown template slot")
        args[key] = _typed(value, properties[key], key, code)
    if row.intent in {"clipboard.paste_history", "clipboard.copy_history", "shelf.check"}:
        key = "index" if tool == "shelf" else "offset"
        if args[key] < 1:
            raise ValueError("one-based index required")
        args[key] -= 1
    if (row.intent.endswith((".play_track", ".play_search", ".playlist", ".music_bot_play"))
            and _MODE_WORD186.fullmatch(str(args.get("query", "") or args.get("playlist", "") or "").strip())):
        # MEDIA185-U03: «включи перемешивание/повтор» is a mode command, not a
        # track called «перемешивание».
        raise ValueError("mode word is not a media payload")
    if row.intent == "date_info.date_info":
        args["text"] = raw
    # NEO 169: «объясни эту ошибку» — the request itself is the question about the screen.
    if row.intent in {"screen.analyze", "screen.analyze_window"} and "instruction" in properties and "instruction" not in args:
        args["instruction"] = raw
    tool, args = normalize_tool_call(tool, args)
    if validate(args, schema):
        raise ValueError("schema mismatch")
    # Both public names describe the same file lookup. Bind this shared phrase
    # to the dedicated search provider; do not select whichever pack loads first.
    if tool == "filesystem" and args.get("action") == "find":
        tool, args = "search", {"action": "files", "query": args.get("name", args.get("query", ""))}
    if row.intent == "browser.open":
        tool, args = "application", {"action": "open", "kind": "app", "name": str(args["browser"])}
    return tool, args


def parse_canonical_command(text: str, code: str, schema_for: Callable[[str], dict]) -> TemplateMatch | None:
    from jarvis.runtime.streaming_commands184 import parse_obs_command
    obs = parse_obs_command(text, code)
    if obs is not None:
        tool, arguments = obs
        if validate(arguments, schema_for(tool)):
            return TemplateMatch(error="invalid_slots")
        return TemplateMatch(tool, arguments, tool + "." + arguments["action"])
    result = _parse_canonical_command(text, code, schema_for)
    # NEO 184: «click at coordinates 10 10!» — the closing mark of the sentence is not part of a number.
    if result is not None and result.error == "invalid_slots":
        stripped = re.sub(r"[\s!?.…]+$", "", str(text or ""))
        if stripped and stripped != str(text or "").strip():
            retry = _parse_canonical_command(stripped, code, schema_for)
            if retry is not None and not retry.error:
                return retry
    return result


def _parse_canonical_command(text: str, code: str, schema_for: Callable[[str], dict]) -> TemplateMatch | None:
    """Return None for no grammar match, or a fail-closed clarification result.

    Equal-specificity collisions between distinct actions are explicit errors.
    A longer literal shell wins over a generic payload shell (e.g. file content
    search versus a filename); insertion order never chooses an action.
    """
    raw = str(text or "").strip().lstrip("¿¡")
    if not raw or len(raw) > 32768 or negated_action(raw, code):
        return None
    if raw[0] in {'"', "'", "«", "“"}:
        return None
    # A task frame belongs to the instruction shell, never to a captured
    # filename/app/target. Normalize before matching a generic payload template.
    raw = strip_request_shell(raw, code).strip()
    if code == 'ru':
        # Polite plural forms of these command heads have identical intent.
        # Normalize the leading verb only; names and message bodies are literal.
        polite_heads = {'покажите': 'покажи', 'откройте': 'открой', 'найдите': 'найди'}
        head = re.match(r'^(\S+)(?=\s)', raw)
        if head and head[1].casefold() in polite_heads:
            raw = polite_heads[head[1].casefold()] + raw[head.end():]
    matched = _matching_templates(raw, code)
    if not matched:
        return None
    if _SLOT.search(raw):
        return TemplateMatch(error="unfilled_template")
    # Preserve the mature compound planner. Text-writing/translation payloads
    # are exempt: conjunctions inside their content are ordinary data.
    payload_tools = {"translate", "text_edit", "clipboard", "input_control", "ui", "copy", "browser_nav", "shelf", "overlay", "email", "telegram_messages"}
    if any(row.intent.split(".", 1)[0] not in payload_tools and any(re.search(_COMPOUND.get(code, r"(?!)"), value, re.I) for value in match.groupdict().values()) for row, match in matched):
        return None
    if any(row.intent.startswith("application.") and re.search(r"\s+(?:и|and|und|et|y|e)\s+", str(match.groupdict().get("name", "")), re.I) for row, match in matched):
        return None
    specificity = max(row.specificity for row, _ in matched)
    candidates: dict[str, TemplateMatch] = {}
    errors = []
    for row, match in matched:
        if row.intent.startswith("obs_control."):
            # Only the literal original-input OBS grammar may bind this tool.
            continue
        if row.specificity != specificity:
            continue
        try:
            tool, args = _bind(row, match.groupdict(), schema_for(row.intent.split(".", 1)[0]), raw, code)
            if validate(args, schema_for(tool)):
                raise ValueError("normalized schema mismatch")
            key = tool + json.dumps(args, ensure_ascii=False, sort_keys=True)
            candidates[key] = TemplateMatch(tool, args, row.intent)
        except (KeyError, ValueError, TypeError, OverflowError):
            errors.append(row.intent)
    if errors:
        return TemplateMatch(error="invalid_slots", candidates=tuple(sorted(set(errors))))
    if len(candidates) != 1:
        return TemplateMatch(error="ambiguous", candidates=tuple(sorted(value.intent for value in candidates.values())))
    return next(iter(candidates.values()))
