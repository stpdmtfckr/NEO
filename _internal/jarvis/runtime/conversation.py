"""Bounded per-conversation state and explicit, persistent personal aliases."""
from __future__ import annotations
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import json
import time
import threading
from typing import Any
from jarvis.runtime.natural_commands import norm


@dataclass
class ConversationState:
    touched: float = field(default_factory=time.monotonic)
    pending_shell: str = ""
    pending_until: float = 0.0
    pending_alias: dict[str, Any] | None = None
    pending_intent: Any = None
    last_attempt: dict[str, Any] = field(default_factory=dict)
    language: str = "ru"
    last_reply: str = ""
    last_tool: str = ""
    last_arguments: dict[str, Any] = field(default_factory=dict)
    legacy: dict[str, Any] = field(default_factory=dict)
    objects157: list[dict[str, Any]] = field(default_factory=list)
    media_owner157: dict[str, Any] = field(default_factory=dict)
    timer_units188: dict[str, Any] = field(default_factory=dict)
    timer_scale188: dict[str, Any] = field(default_factory=dict)


class ConversationStore:
    def __init__(self, *, limit: int = 32, ttl: float = 1800) -> None:
        self.limit, self.ttl = limit, ttl
        self._states: OrderedDict[str, ConversationState] = OrderedDict()
        self.lock = threading.RLock()

    def get(self, key: str, *, now: float | None = None) -> ConversationState:
        with self.lock:
            return self._get_locked(key, now=now)

    def _get_locked(self, key: str, *, now: float | None = None) -> ConversationState:
        now = time.monotonic() if now is None else now
        state = self._states.pop(key, None)
        if state is None or now - state.touched > self.ttl:
            state = ConversationState(touched=now)
        state.touched = now
        if state.pending_until < now:
            state.pending_shell = ""
            state.pending_alias = None
        self._states[key] = state
        while len(self._states) > self.limit:
            self._states.popitem(last=False)
        return state


class PersonalAliases:
    """Alias -> native command, never directly to an unchecked tool call.

    Proposals are not executable. The UI/Agent must show the expansion and obtain
    explicit confirmation before add(); calling an alias re-enters normal risk,
    locale, ownership and argument checks. Values use the existing SQLite store.
    """
    def __init__(self, database: Any) -> None:
        self.database = database
        self._cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._cache_raw: dict[str, str] = {}

    def _rows(self, code: str) -> dict[str, dict[str, Any]]:
        # Read the indexed record so other agents/connections cannot keep a
        # deleted alias alive. Decode only when the stored value changed.
        raw = self.database.get_memory("personal_aliases." + code, "{}")
        if code not in self._cache or self._cache_raw.get(code) != raw:
            try:
                data = json.loads(raw)
                self._cache[code] = data if isinstance(data, dict) else {}
            except (TypeError, ValueError):
                self._cache[code] = {}
            self._cache_raw[code] = raw
        return self._cache[code]

    def rows(self, code: str) -> dict[str, dict[str, Any]]:
        return deepcopy(self._rows(code))

    def resolve(self, phrase: str, code: str) -> str:
        row = self._rows(code).get(norm(phrase), {})
        if not isinstance(row, dict):
            return ""
        return str(row.get("command", "")) if row.get("enabled", False) else ""

    def resolve_record(self, phrase: str, code: str) -> dict[str, Any]:
        """Capture command and optional target binding from one fresh store read."""
        row = self._rows(code).get(norm(phrase), {})
        return deepcopy(row) if isinstance(row, dict) and row.get("enabled", False) else {}

    def add(self, phrase: str, command: str, code: str, *, confirmed: bool = False, target: dict | None = None) -> None:
        if not confirmed:
            raise ValueError("Alias creation requires explicit confirmation")
        # Keep the exact payload shown for approval; normalize only its lookup key.
        phrase, command = str(phrase), str(command)
        key = norm(phrase)
        if not key or len(key) > 120 or not command.strip() or len(command) > 1000:
            raise ValueError("Invalid alias length")
        if key == norm(command) or key in {"да", "нет", "yes", "no", "stop", "стоп", "cancel", "отмена"}:
            raise ValueError("Reserved or recursive alias")
        if target is not None and (not isinstance(target, dict) or set(target) != {"kind", "name"}
                or target.get("kind") != "youtube_playlist" or not isinstance(target.get("name"), str)
                or not target["name"].strip() or len(target["name"]) > 100):
            raise ValueError("Invalid alias target")
        rows = self.rows(code)
        if key not in rows and len(rows) >= 200:
            raise ValueError("Alias limit reached")
        rows[key] = {"phrase": phrase, "command": command, "enabled": True}
        if target is not None:
            rows[key]["target"] = deepcopy(target)
        self._save(code, rows)

    def set_enabled(self, phrase: str, code: str, enabled: bool) -> bool:
        rows = self.rows(code)
        row = rows.get(norm(phrase))
        if not isinstance(row, dict):
            return False
        row["enabled"] = bool(enabled)
        self._save(code, rows)
        return True

    def delete(self, phrase: str, code: str) -> bool:
        rows = self.rows(code)
        found = rows.pop(norm(phrase), None) is not None
        if found:
            self._save(code, rows)
        return found

    def _save(self, code: str, rows: dict[str, dict[str, Any]]) -> None:
        raw = json.dumps(rows, ensure_ascii=False)
        self.database.set_memory("personal_aliases." + code, raw)
        # Publish to the cache only after the database confirms the write.
        self._cache[code] = deepcopy(rows)
        self._cache_raw[code] = raw
