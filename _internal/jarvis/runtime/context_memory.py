# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Durable local context journal for NEO JARVIS.

The existing SQLite database remains the source of truth for security/action
history.  This module adds a human-readable JSON/JSONL context layer requested by
users: recent requests, verified tool actions, lightweight object pointers and
explicit memories survive restarts and can be fed back into the local LLM.

Files are stored under ``<data_dir>/context_memory`` and never leave the machine.
Potential credentials are redacted before they are written to the JSON journal.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_SECRET_KEY_RE = re.compile(
    r"(?:token|password|passwd|secret|authorization|cookie|api[_-]?key|bot[_-]?token|access[_-]?token|refresh[_-]?token)",
    re.I,
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}", re.I),
    # Common Discord-style token shape. Keep this deliberately conservative.
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)(\b(?:парол(?:ь|я)|password|token|secret|api\s*key)\s*[:=]\s*)([^\s,;]+)"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _safe_slug(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[^0-9a-zа-яё._-]+", "_", text, flags=re.I).strip("._-")
    return (text[:80] or fallback)


def _clip(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _redact_text(value: Any) -> str:
    # NEO 188 NEO-PRIVACY-003: the whole text is redacted BEFORE it is cut.
    from jarvis.runtime.diagnostics import clip_redacted
    text = clip_redacted(str(value or ""), 4000)
    for pattern in _SECRET_TEXT_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "<redacted>", text)
        else:
            text = pattern.sub("<redacted>", text)
    return text


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return _redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:80]:
            key = _clip(raw_key, 120)
            low_key = key.casefold()
            # Spectrum/PCM arrays are useful only while rendering the live player.
            # Persisting them on every status poll bloats monthly journals without
            # improving conversational context.
            if low_key in {"levels", "audio_levels", "spectrum", "waveform", "pcm", "audio_b64"}:
                continue
            if _SECRET_KEY_RE.search(key):
                out[key] = "<redacted>"
            elif low_key in {"url", "stream_url", "audio_url"} and isinstance(raw_value, str) and len(raw_value) > 320:
                out[key] = "<omitted long media URL>"
            else:
                out[key] = _sanitize(raw_value, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:80]]
    if isinstance(value, (str, Path)):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value)


class ContextMemory:
    """Thread-safe, append-only local JSON context memory.

    Multiple Agent instances (for example local voice + Telegram remote) share
    one lock per data directory so atomic ``*.tmp`` writes cannot race each other.
    """

    _shared_locks_guard = threading.Lock()
    _shared_locks: dict[str, threading.RLock] = {}
    _shared_state_cache: dict[str, dict[str, Any]] = {}
    _shared_state_mtime_ns: dict[str, int] = {}

    def __init__(self, config: Any) -> None:
        self.config = config
        try:
            root = Path(getattr(getattr(config, "paths", None), "root", Path.home() / ".neo_jarvis")).resolve(strict=False)
            key = str(root).casefold() if os.name == "nt" else str(root)
        except Exception:
            key = "default"
        with self._shared_locks_guard:
            self._lock = self._shared_locks.setdefault(key, threading.RLock())
        self._cache_key = key
        base_root = Path(getattr(getattr(config, "paths", None), "root", Path.home() / ".neo_jarvis"))
        self._root_path = base_root / "context_memory"
        self._journal_root = self._root_path / "journal"
        self._system_root_path = self._root_path / "system"
        self._gaming_root_path = self._root_path / "gaming"
        # Directory creation is a startup cost, not a per-message filesystem syscall.
        self._journal_root.mkdir(parents=True, exist_ok=True)
        (self._system_root_path / "activity").mkdir(parents=True, exist_ok=True)
        (self._system_root_path / "actions").mkdir(parents=True, exist_ok=True)
        self._gaming_root_path.mkdir(parents=True, exist_ok=True)

    def enabled(self) -> bool:
        try:
            return bool(self.config.get("context_memory.enabled", True))
        except Exception:
            return True

    def _root(self) -> Path:
        return self._root_path

    def _state_path(self) -> Path:
        return self._root() / "state.json"

    def _profile_path(self) -> Path:
        return self._root() / "profile.json"

    def _journal_path(self, kind: str, month: str | None = None) -> Path:
        suffix = {"dialogue": "dialogue", "speech": "speech"}.get(kind, "actions")
        return self._root() / "journal" / f"{month or _month_key()}_{suffix}.jsonl"

    def _system_root(self) -> Path:
        return self._system_root_path

    def _gaming_root(self) -> Path:
        return self._gaming_root_path

    def _game_root(self, game_name: str) -> Path:
        root = self._gaming_root() / _safe_slug(game_name, "unknown_game")
        (root / "actions").mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"at": _utc_now(), **_sanitize(payload)}, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            if not path.exists():
                return default
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _atomic_write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def _load_state_cached(self) -> dict[str, Any]:
        path = self._state_path()
        try:
            mtime_ns = int(path.stat().st_mtime_ns)
        except OSError:
            mtime_ns = -1
        cached = self._shared_state_cache.get(self._cache_key)
        cached_mtime = self._shared_state_mtime_ns.get(self._cache_key, -2)
        if cached is not None and cached_mtime == mtime_ns:
            return cached
        raw = self._read_json(path, {}) if mtime_ns >= 0 else {}
        state = raw if isinstance(raw, dict) else {}
        self._shared_state_cache[self._cache_key] = state
        self._shared_state_mtime_ns[self._cache_key] = mtime_ns
        return state

    def _write_state_cached(self, state: dict[str, Any]) -> None:
        # Store one shared in-process snapshot for desktop + Telegram Agent
        # instances, then persist with the same atomic replace semantics as before.
        snapshot = dict(state)
        path = self._state_path()
        self._atomic_write_json(path, snapshot)
        self._shared_state_cache[self._cache_key] = snapshot
        try:
            self._shared_state_mtime_ns[self._cache_key] = int(path.stat().st_mtime_ns)
        except OSError:
            self._shared_state_mtime_ns[self._cache_key] = -1

    def state_snapshot(self) -> dict[str, Any]:
        if not self.enabled():
            return {}
        with self._lock:
            return dict(self._load_state_cached())

    def update_state(self, **values: Any) -> None:
        if not self.enabled():
            return
        with self._lock:
            state = dict(self._load_state_cached())
            for key, value in values.items():
                if value in (None, ""):
                    continue
                state[str(key)] = _sanitize(value)
            state["updated_at"] = _utc_now()
            self._write_state_cached(state)

    def clear_state(self, *keys: str) -> None:
        """Remove stale referents after a confirmed delete/close operation."""
        if not self.enabled() or not keys:
            return
        with self._lock:
            state = dict(self._load_state_cached())
            changed = False
            for key in keys:
                if str(key) in state:
                    state.pop(str(key), None)
                    changed = True
            if changed:
                state["updated_at"] = _utc_now()
                self._write_state_cached(state)

    def _append(self, kind: str, payload: dict[str, Any]) -> None:
        if not self.enabled():
            return
        path = self._journal_path(kind)
        row = {"at": _utc_now(), **_sanitize(payload)}
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def record_user(self, text: str) -> None:
        if not self.enabled() or not str(text or "").strip():
            return
        if bool(self.config.get("context_memory.persist_dialogue", True)):
            self._append("dialogue", {"role": "user", "content": _redact_text(text)})
        self.update_state(last_user_request=_redact_text(text), last_interaction_at=_utc_now())

    def record_speech_resolution(self, payload: dict[str, Any]) -> None:
        """Persist raw and resolved ASR separately from the visible dialogue.

        The chat/history may use the understood form while this bounded-purpose
        journal preserves what Whisper actually heard for diagnostics and future
        personalized learning. Secrets are sanitized by the normal journal path.
        """
        if not self.enabled() or not isinstance(payload, dict):
            return
        raw = str(payload.get("raw", "") or "").strip()
        text = str(payload.get("text", "") or "").strip()
        if not raw and not text:
            return
        if bool(self.config.get("context_memory.persist_actions", True)):
            self._append("speech", {
                "raw": _redact_text(raw),
                "resolved": _redact_text(text),
                "changed": bool(payload.get("changed", False)),
                "selected_hypothesis": str(payload.get("selected_hypothesis", "") or ""),
                "score": payload.get("score", 0.0),
                "corrections": payload.get("corrections", []),
            })

    def record_assistant(self, text: str) -> None:
        if not self.enabled() or not str(text or "").strip():
            return
        if bool(self.config.get("context_memory.persist_dialogue", True)):
            self._append("dialogue", {"role": "assistant", "content": _redact_text(text)})
        self.update_state(last_assistant_reply=_redact_text(text), last_interaction_at=_utc_now())

    def record_action(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        success: bool,
        result_text: str,
        data: Any = None,
    ) -> None:
        if not self.enabled():
            return
        safe_args = _sanitize(arguments if isinstance(arguments, dict) else {})
        safe_data = _sanitize(data)
        if bool(self.config.get("context_memory.persist_actions", True)):
            self._append(
                "actions",
                {
                    "tool": str(tool_name or ""),
                    "arguments": safe_args,
                    "success": bool(success),
                    "result": _redact_text(result_text),
                    "data": safe_data,
                },
            )
        if not success:
            return

        state: dict[str, Any] = {
            "last_tool": str(tool_name or ""),
            "last_action": safe_args,
            "last_action_result": _redact_text(result_text),
        }
        args = arguments if isinstance(arguments, dict) else {}
        payload = data if isinstance(data, dict) else {}
        tool = str(tool_name or "").casefold()
        action = str(args.get("action", "") or "").casefold()

        path_value = str(payload.get("path", "") or (payload.get("value", "") if payload.get("kind") == "path" else "") or "").strip()
        if tool in {"filesystem", "desktop"} and path_value:
            try:
                p = Path(path_value)
                if tool == "filesystem" and action in {"delete", "delete_permanent"}:
                    current_state = self.state_snapshot()
                    stale = []
                    if str(current_state.get("last_file_path", "")) == str(p):
                        stale.append("last_file_path")
                    if str(current_state.get("last_folder_path", "")) == str(p):
                        stale.append("last_folder_path")
                    if stale:
                        self.clear_state(*stale)
                    if p.suffix:
                        state["last_folder_path"] = str(p.parent)
                elif p.suffix:
                    state["last_file_path"] = str(p)
                    state["last_folder_path"] = str(p.parent)
                else:
                    state["last_folder_path"] = str(p)
            except Exception:
                pass

        payload_kind = str(payload.get("kind", "") or "").casefold()
        looks_like_process = tool == "application" or (
            tool == "desktop" and (payload_kind in {"app", "game"} or bool(payload.get("executable")) or bool(payload.get("pid")))
        )
        if looks_like_process:
            label = str(
                payload.get("label", "") or payload.get("name", "")
                or args.get("name", "") or args.get("query", "") or ""
            ).strip()
            if label:
                state["last_application"] = label
                state["last_process_request"] = label
                detail = {
                    "label": label,
                    "pid": payload.get("pid"),
                    "executable": str(payload.get("executable", "") or ""),
                    "kind": payload_kind or str(args.get("kind", "") or ""),
                    "at": _utc_now(),
                }
                state["last_process"] = detail
                current_state = self.state_snapshot()
                current = current_state.get("recent_processes", [])
                rows = [str(x) for x in current if str(x).strip()] if isinstance(current, list) else []
                rows = [label] + [x for x in rows if x.casefold() != label.casefold()]
                state["recent_processes"] = rows[:16]
                details = current_state.get("recent_process_details", [])
                details = [x for x in details if isinstance(x, dict)] if isinstance(details, list) else []
                details = [detail] + [x for x in details if str(x.get("label", "")).casefold() != label.casefold()]
                state["recent_process_details"] = details[:16]
        if tool == "game":
            game = str(payload.get("game_name", "") or payload.get("name", "") or args.get("game", "") or args.get("name", "") or "").strip()
            if game:
                state["last_game_name"] = game
        if tool == "overlay":
            name = str(payload.get("name", "") or args.get("name", "") or args.get("title", "") or "").strip()
            if name and action in {"delete", "remove"}:
                current_state = self.state_snapshot()
                stale = []
                for key in ("last_overlay_name", "last_checklist_name", "last_notes_name"):
                    if str(current_state.get(key, "")).casefold() == name.casefold():
                        stale.append(key)
                if stale:
                    self.clear_state(*stale)
            elif name:
                state["last_overlay_name"] = name
                joined = " ".join(str(args.get(k, "") or "") for k in ("kind", "purpose", "action")).casefold()
                if "checklist" in joined:
                    state["last_checklist_name"] = name
                if "note" in joined or "append_text" in joined or "замет" in joined:
                    state["last_notes_name"] = name
        if tool == "discord":
            state["last_integration_target"] = "discord"
            if action.startswith("music_"):
                state["last_media_target"] = "discord"
            server = str(payload.get("server", "") or args.get("server", "") or "").strip()
            channel = str(payload.get("channel", "") or args.get("channel", "") or "").strip()
            if server:
                state["last_discord_server"] = server
            if channel:
                state["last_discord_channel"] = channel
        if tool == "yandex_music":
            state["last_integration_target"] = "yandex_music"
            state["last_media_target"] = "yandex_music"
        if tool == "media" and str(args.get("target", "") or "").casefold() in {"youtube", "video"}:
            state["last_media_target"] = "youtube"
        if tool in {"browser", "browser_advanced"}:
            title = str(payload.get("title", "") or "").strip()
            if title:
                state["last_browser_title"] = title
        self.update_state(**state)

    def system_snapshot(self) -> dict[str, Any]:
        """Return the dedicated long-term system context snapshot."""
        if not self.enabled():
            return {}
        with self._lock:
            raw = self._read_json(self._system_root() / "state.json", {})
            return raw if isinstance(raw, dict) else {}

    def game_snapshot(self, game_name: str = "") -> dict[str, Any]:
        """Return persistent context for one game (defaults to the last game)."""
        if not self.enabled():
            return {}
        name = str(game_name or self.state_snapshot().get("last_game_name", "") or "").strip()
        if not name:
            return {}
        with self._lock:
            raw = self._read_json(self._game_root(name) / "state.json", {})
            return raw if isinstance(raw, dict) else {}

    def record_system_observation(
        self, *, process_name: str, title: str, pid: int = 0, hwnd: int = 0, folder_path: str = ""
    ) -> None:
        """Persist passive foreground context even when JARVIS did not cause it.

        The tracker calls this only on meaningful foreground/folder changes, so
        the journal remains small instead of becoming a high-frequency activity log.
        """
        if not self.enabled():
            return
        process = str(process_name or "").strip()
        title = str(title or "").strip()
        folder = str(folder_path or "").strip()
        if not (process or title or folder):
            return
        now = _utc_now()
        item = {"process": process, "title": title, "pid": int(pid or 0), "hwnd": int(hwnd or 0), "at": now}
        if folder:
            item["folder"] = folder
        with self._lock:
            path = self._system_root() / "state.json"
            state = self._read_json(path, {})
            if not isinstance(state, dict):
                state = {}
            old = state.get("current_window") if isinstance(state.get("current_window"), dict) else {}
            same_window = (
                int(old.get("hwnd", 0) or 0) == int(hwnd or 0)
                and str(old.get("process", "")) == process
                and str(old.get("title", "")) == title
                and str(old.get("folder", "")) == folder
            )
            if same_window:
                return
            windows = state.get("recent_windows", [])
            windows = [x for x in windows if isinstance(x, dict)] if isinstance(windows, list) else []
            key = (process.casefold(), title.casefold(), folder.casefold())
            windows = [item] + [x for x in windows if (str(x.get("process", "")).casefold(), str(x.get("title", "")).casefold(), str(x.get("folder", "")).casefold()) != key]
            state["current_window"] = item
            state["recent_windows"] = windows[:40]
            if process:
                processes = state.get("recent_processes", [])
                processes = [str(x) for x in processes if str(x).strip()] if isinstance(processes, list) else []
                processes = [process] + [x for x in processes if x.casefold() != process.casefold()]
                state["recent_processes"] = processes[:30]
            if folder:
                folders = state.get("recent_folders", [])
                folders = [str(x) for x in folders if str(x).strip()] if isinstance(folders, list) else []
                folders = [folder] + [x for x in folders if x.casefold() != folder.casefold()]
                state["recent_folders"] = folders[:30]
                state["last_folder"] = folder
            state["updated_at"] = now
            self._atomic_write_json(path, state)
            self._append_jsonl(self._system_root() / "activity" / f"{_month_key()}.jsonl", {"event": "foreground_changed", **item})

    def record_game_session_event(self, snapshot: dict[str, Any], *, status: str = "running") -> None:
        """Persist process-verified game session state in the gaming memory tree."""
        if not self.enabled() or not isinstance(snapshot, dict):
            return
        game_name = str(snapshot.get("game_name", "") or snapshot.get("game_id", "") or "Unknown game").strip()
        game_id = str(snapshot.get("game_id", "") or _safe_slug(game_name, "generic"))
        pid = int(snapshot.get("pid", 0) or 0)
        started_at = float(snapshot.get("session_started_at", 0.0) or 0.0)
        status_key = str(status or "running").strip().casefold()
        now = _utc_now()
        row = {
            "event": f"session_{status_key}", "game_id": game_id, "game_name": game_name,
            "pid": pid, "process_name": str(snapshot.get("process_name", "") or ""),
            "executable": str(snapshot.get("executable", "") or ""),
            "session_started_at": started_at, "session_seconds": int(snapshot.get("session_seconds", 0) or 0),
            "status": status_key,
        }
        with self._lock:
            root = self._game_root(game_name)
            state_path = root / "state.json"
            state = self._read_json(state_path, {})
            if not isinstance(state, dict): state = {}
            previous = state.get("current_session") if isinstance(state.get("current_session"), dict) else {}
            session_key = f"{pid}:{int(started_at)}"
            previous_key = f"{int(previous.get('pid',0) or 0)}:{int(float(previous.get('session_started_at',0) or 0))}" if previous else ""
            session = {**row, "session_key": session_key, "last_seen_at": now}
            state["game_id"] = game_id
            state["game_name"] = game_name
            state["last_seen_at"] = now
            state["current_session"] = session if status_key == "running" else {}
            state["last_session"] = session
            self._atomic_write_json(state_path, state)
            # Journal only lifecycle boundaries; recurring running heartbeats update
            # state.json in place and do not grow the JSONL file.
            should_append = status_key != "running" or previous_key != session_key
            if should_append:
                self._append_jsonl(root / "sessions" / f"{_month_key()}.jsonl", row)
            index_path = self._gaming_root() / "index.json"
            index = self._read_json(index_path, {})
            if not isinstance(index, dict): index = {}
            index[_safe_slug(game_name, "unknown_game")] = {"name": game_name, "game_id": game_id, "last_seen_at": now}
            self._atomic_write_json(index_path, index)
        self.update_state(last_game_name=game_name)

    def record_domain_action(
        self, *, domain: str, intent: str, tool_name: str, arguments: dict[str, Any],
        success: bool, result_text: str, data: Any = None, transport: str = "local"
    ) -> None:
        """Write canonical domain metadata and split system/game memories."""
        if not self.enabled():
            return
        domain_key = str(domain or "general").strip().casefold() or "general"
        payload = data if isinstance(data, dict) else {}
        args = arguments if isinstance(arguments, dict) else {}
        row = {
            "domain": domain_key, "intent": str(intent or ""), "tool": str(tool_name or ""),
            "transport": str(transport or "local"), "arguments": _sanitize(args),
            "success": bool(success), "result": _redact_text(result_text), "data": _sanitize(data),
        }
        # Store the last domain in the general state so references keep their
        # semantic owner across voice/desktop/Telegram routes.
        self.update_state(last_domain=domain_key, last_domain_intent=str(intent or ""))
        if domain_key == "system":
            with self._lock:
                root = self._system_root()
                self._append_jsonl(root / "actions" / f"{_month_key()}.jsonl", row)
                state = self._read_json(root / "state.json", {})
                if not isinstance(state, dict): state = {}
                state["last_action"] = row
                state["updated_at"] = _utc_now()
                self._atomic_write_json(root / "state.json", state)
            return
        if domain_key != "gaming":
            return
        game_name = str(
            payload.get("game_name", "") or payload.get("game", "") or
            (payload.get("name", "") if str(payload.get("kind", "")).casefold() == "game" else "") or
            args.get("game", "") or (args.get("name", "") if str(args.get("kind", "")).casefold() == "game" else "") or
            self.state_snapshot().get("last_game_name", "") or "Unknown game"
        ).strip()
        with self._lock:
            root = self._game_root(game_name)
            self._append_jsonl(root / "actions" / f"{_month_key()}.jsonl", {"game": game_name, **row})
            state = self._read_json(root / "state.json", {})
            if not isinstance(state, dict): state = {}
            state["game_name"] = game_name
            state["last_seen_at"] = _utc_now()
            state["last_action"] = row
            recent = state.get("recent_actions", [])
            recent = [x for x in recent if isinstance(x, dict)] if isinstance(recent, list) else []
            state["recent_actions"] = [row] + recent[:29]
            self._atomic_write_json(root / "state.json", state)
            index_path = self._gaming_root() / "index.json"
            index = self._read_json(index_path, {})
            if not isinstance(index, dict): index = {}
            index[_safe_slug(game_name, "unknown_game")] = {"name": game_name, "last_seen_at": _utc_now()}
            self._atomic_write_json(index_path, index)
        self.update_state(last_game_name=game_name)

    def sync_profile(self, memories: Iterable[dict[str, Any]]) -> None:
        """Mirror explicit JARVIS memories to a readable profile JSON file."""
        if not self.enabled():
            return
        rows: list[dict[str, Any]] = []
        for item in list(memories)[:200]:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "key": _redact_text(item.get("key", "")),
                    "value": _redact_text(item.get("value", "")),
                    "updated_at": _redact_text(item.get("updated_at", "")),
                }
            )
        with self._lock:
            self._atomic_write_json(self._profile_path(), {"updated_at": _utc_now(), "memories": rows})

    @staticmethod
    def _tail_text_lines(path: Path, limit: int, *, max_bytes: int = 1024 * 1024) -> list[str]:
        """Read only the tail of an append-only JSONL journal.

        The old code read and split entire monthly journals for every model turn.
        Long-running installations can grow those files to many MB even though the
        prompt needs only a handful of recent rows.
        """
        wanted = max(1, int(limit))
        chunks: list[bytes] = []
        total = 0
        newline_count = 0
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                pos = fh.tell()
                while pos > 0 and total < max_bytes:
                    step = min(64 * 1024, pos, max_bytes - total)
                    pos -= step
                    fh.seek(pos)
                    chunk = fh.read(step)
                    chunks.append(chunk)
                    total += len(chunk)
                    newline_count += chunk.count(b"\n")
                    # Count incrementally. Re-joining all accumulated chunks on
                    # every 64-KiB iteration made tail reads quadratic in copied
                    # bytes exactly when journals were large.
                    if newline_count >= wanted + 2:
                        break
        except OSError:
            return []
        data = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        return data.splitlines()[-wanted:]

    def _recent_rows(self, kind: str, limit: int) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        limit = max(1, min(200, int(limit)))
        files = sorted((self._root() / "journal").glob(f"*_{'dialogue' if kind == 'dialogue' else 'actions'}.jsonl"), reverse=True)
        rows: list[dict[str, Any]] = []
        for path in files[:3]:
            # Read a modest surplus because malformed/redacted rows may be skipped.
            lines = self._tail_text_lines(path, min(600, max(limit * 3, limit + 12)))
            for line in reversed(lines):
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
                if len(rows) >= limit:
                    return rows
        return rows

    def recent_dialogue_messages(self, limit: int = 10) -> list[dict[str, str]]:
        rows = list(reversed(self._recent_rows("dialogue", max(1, limit))))
        out: list[dict[str, str]] = []
        for row in rows:
            role = str(row.get("role", "") or "")
            content = str(row.get("content", "") or "").strip()
            if role in {"user", "assistant"} and content:
                out.append({"role": role, "content": content})
        return out[-limit:]

    def prompt_context(self, *, request_limit: int = 6, action_limit: int = 8) -> str:
        """Compact context block suitable for the local model system prompt."""
        if not self.enabled():
            return ""
        state = self.state_snapshot()
        useful_keys = (
            "last_file_path", "last_folder_path", "last_application", "last_game_name",
            "last_overlay_name", "last_checklist_name", "last_notes_name",
            "last_media_target", "last_integration_target", "last_discord_server",
            "last_discord_channel", "last_process", "recent_processes",
        )
        state_bits = []
        for key in useful_keys:
            value = state.get(key)
            if value not in (None, "", []):
                state_bits.append(f"{key}={_clip(value, 320)}")

        dialogue = self._recent_rows("dialogue", max(2, request_limit * 3))
        requests = [str(row.get("content", "")) for row in dialogue if str(row.get("role", "")) == "user" and str(row.get("content", "")).strip()][:request_limit]
        actions = self._recent_rows("actions", action_limit)
        action_bits = []
        for row in actions:
            if not bool(row.get("success")):
                continue
            args = row.get("arguments") if isinstance(row.get("arguments"), dict) else {}
            details = []
            for key in ("action", "name", "title", "target", "query", "source", "destination", "monitor", "position"):
                value = str(args.get(key, "") or "").strip()
                if value:
                    details.append(f"{key}={value[:80]}")
            action_bits.append(f"{row.get('tool', '?')}({', '.join(details) or 'action'}) -> {str(row.get('result', ''))[:150]}")
            if len(action_bits) >= action_limit:
                break

        parts = []
        system_state = self.system_snapshot()
        if system_state:
            system_bits = []
            current = system_state.get("current_window") if isinstance(system_state.get("current_window"), dict) else {}
            if current:
                system_bits.append(f"active={_clip(current.get('process',''),80)}:{_clip(current.get('title',''),120)}")
            folders = system_state.get("recent_folders", [])
            if isinstance(folders, list) and folders:
                system_bits.append("recent_folders=" + ", ".join(str(x)[:100] for x in folders[:4]))
            processes = system_state.get("recent_processes", [])
            if isinstance(processes, list) and processes:
                system_bits.append("recent_processes=" + ", ".join(str(x)[:60] for x in processes[:6]))
            if system_bits:
                parts.append("Системная долговременная память: " + "; ".join(system_bits))
        game_state = self.game_snapshot()
        if game_state:
            game_name = str(game_state.get("game_name", "") or state.get("last_game_name", "") or "")
            last_game_action = game_state.get("last_action") if isinstance(game_state.get("last_action"), dict) else {}
            game_bits = [f"game={game_name}"] if game_name else []
            if last_game_action:
                game_bits.append(f"last={_clip(last_game_action.get('intent',''),100)}")
            if game_bits:
                parts.append("Игровая долговременная память: " + "; ".join(game_bits))
        if state_bits:
            parts.append("Текущее долговременное состояние: " + "; ".join(state_bits))
        if requests:
            parts.append("Недавние запросы пользователя: " + " | ".join(reversed(requests)))
        if action_bits:
            parts.append("Недавние подтверждённые действия: " + " | ".join(action_bits))
        return " ".join(parts)
