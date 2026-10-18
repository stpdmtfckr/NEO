# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Persistent, thread-safe state for small JARVIS information overlays.

The Agent/tools run on worker threads while Qt widgets must be touched only from
its GUI thread.  This module is intentionally Qt-free: tools modify an atomic
JSON model and publish a tiny refresh event.  MainWindow polls those events and
lets OverlayManager render the new model on the GUI thread.
"""

import json
import queue
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

_REFRESH_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
_LOCK = threading.RLock()


def publish_overlay_refresh(name: str = "") -> None:
    event = {"type": "refresh", "name": str(name or "")}
    try:
        _REFRESH_QUEUE.put_nowait(event)
    except queue.Full:
        # Refreshes are level-triggered: keeping the newest event is more useful
        # than allowing stale bursts to grow without bound.
        try:
            _REFRESH_QUEUE.get_nowait()
        except queue.Empty:
            pass
        try:
            _REFRESH_QUEUE.put_nowait(event)
        except queue.Full:
            pass


def drain_overlay_events(limit: int = 64) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for _ in range(max(1, int(limit))):
        try:
            events.append(_REFRESH_QUEUE.get_nowait())
        except queue.Empty:
            break
    return events


def _safe_name(value: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    return name[:80] or "Информация"


def _normalize_color(value: Any) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw.upper()
    key = raw.casefold().replace("ё", "е")
    colors = {
        "white": "#FFFFFF", "black": "#000000", "red": "#FF4D5A",
        "green": "#4ADE80", "blue": "#4DA3FF", "cyan": "#57D7FF",
        "yellow": "#FFD166", "orange": "#FF9F43", "purple": "#A78BFA",
        "gray": "#9CA3AF", "grey": "#9CA3AF",
    }
    if key in colors:
        return colors[key]
    stems = [
        ("бел", "#FFFFFF"), ("черн", "#000000"), ("красн", "#FF4D5A"),
        ("зелен", "#4ADE80"), ("зелён", "#4ADE80"), ("син", "#4DA3FF"),
        ("голуб", "#57D7FF"), ("бирюз", "#57D7FF"), ("желт", "#FFD166"),
        ("жёлт", "#FFD166"), ("оранж", "#FF9F43"), ("фиолет", "#A78BFA"),
        ("сер", "#9CA3AF"),
    ]
    for stem, color in stems:
        if key.startswith(stem):
            return color
    # Keep CSS-compatible English/hex/rgb strings rather than silently changing
    # an advanced value the user entered in settings.
    return raw or "#FFFFFF"


def _default_style() -> dict[str, Any]:
    return {
        "font_family": "Segoe UI",
        "font_size": 18,
        "text_color": "#F2F6FF",
        "background_color": "#0A0F18",
        "border_color": "#57D7FF",
        # ``opacity`` remains as a compatibility mirror for old overlay files.
        "opacity": 72,
        "background_opacity": 72,
        "border_opacity": 100,
        "border_width": 1,
        "padding": 14,
        "width": 420,
        # 0 = automatic height based on content. A positive value is an explicit
        # user-selected panel height and can be changed by voice.
        "height": 0,
        "click_through": True,
        # Dragging is deliberately independent from click-through. A panel can
        # be interactable yet locked, and drag starts only from its top handle.
        "drag_enabled": True,
    }


def _normalize_position(value: str) -> str:
    aliases = {
        "left_top": "top_left", "верхний левый": "top_left", "левый верхний": "top_left",
        "слева вверху": "top_left", "вверху слева": "top_left",
        "right_top": "top_right", "верхний правый": "top_right", "правый верхний": "top_right",
        "справа вверху": "top_right", "вверху справа": "top_right",
        "left_bottom": "bottom_left", "нижний левый": "bottom_left", "левый нижний": "bottom_left",
        "слева внизу": "bottom_left", "внизу слева": "bottom_left",
        "right_bottom": "bottom_right", "нижний правый": "bottom_right", "правый нижний": "bottom_right",
        "справа внизу": "bottom_right", "внизу справа": "bottom_right",
        "слева посередине": "center_left", "слева по центру": "center_left",
        "справа посередине": "center_right", "справа по центру": "center_right",
        "сверху посередине": "top_center", "вверху посередине": "top_center", "сверху по центру": "top_center",
        "снизу посередине": "bottom_center", "внизу посередине": "bottom_center", "снизу по центру": "bottom_center",
        "top": "top_center", "bottom": "bottom_center", "middle": "center", "центр": "center",
        "посередине": "center", "по центру": "center", "в центре": "center",
    }
    raw = str(value or "top_left").strip().casefold().replace("-", "_").replace(" ", "_")
    raw = aliases.get(str(value or "").strip().casefold(), aliases.get(raw.replace("_", " "), raw))
    allowed = {
        "top_left", "top_center", "top_right", "center_left", "center", "center_right",
        "bottom_left", "bottom_center", "bottom_right",
    }
    return raw if raw in allowed else "top_left"


class OverlayStore:
    def __init__(self, path: Path, defaults: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.defaults = deepcopy(defaults or {})
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 4, "panels": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            panels = data.get("panels")
            if not isinstance(panels, dict):
                data["panels"] = {}
            
            version = int(data.get("version", 2) or 2)
            if version < 4:
                # v2.6 one-time migration: older game-session HUDs were too narrow
                # for live information. Enlarge only legacy default-width panels;
                # once migrated, user-selected smaller widths remain respected.
                for panel in data.get("panels", {}).values():
                    if not isinstance(panel, dict) or str(panel.get("purpose", "")) != "game_session":
                        continue
                    style = panel.setdefault("style", {})
                    try:
                        old_width = int(style.get("width", 360) or 360)
                    except Exception:
                        old_width = 360
                    if old_width <= 360:
                        style["width"] = 420
                    style.setdefault("height", 0)
                data["version"] = 4
            else:
                data.setdefault("version", 4)
            return data
        except Exception:
            # Never destroy the user's file if it became malformed. Start with an
            # empty in-memory model; the next successful mutation is atomic.
            return {"version": 4, "panels": {}}

    def load(self) -> dict[str, Any]:
        with _LOCK:
            return deepcopy(self._load_unlocked())

    @staticmethod
    def _style_category(panel: Any) -> str:
        if not isinstance(panel, dict):
            return "default"
        purpose = str(panel.get("purpose", "") or "").casefold()
        kind = str(panel.get("kind", "text") or "text").casefold()
        if purpose == "game_session":
            return "session"
        if kind == "checklist" or "checklist" in purpose:
            return "checklist"
        if purpose == "music":
            return "music"
        if purpose == "notes" or "note" in purpose:
            return "notes"
        return "default"

    def clear_category_style_overrides(self, category: str) -> int:
        """Make panels of *category* inherit the canonical Settings style again.

        Telegram's "General settings" edits the same category defaults as the
        desktop Settings dialog. Older/voice-created panels may carry a local
        ``style_override`` flag; leaving it set would mask the newly selected
        preset/font/opacity. Clearing only that flag preserves content, visibility,
        placement overrides and all stored panel data while rejoining the shared
        style configuration.
        """
        wanted = str(category or "").casefold().strip()
        if wanted not in {"session", "music", "checklist", "notes"}:
            return 0
        changed = 0
        with _LOCK:
            data = self._load_unlocked()
            panels = data.setdefault("panels", {})
            for panel in panels.values():
                if not isinstance(panel, dict) or self._style_category(panel) != wanted:
                    continue
                if bool(panel.get("style_override", False)):
                    panel["style_override"] = False
                    panel["updated_at"] = time.time()
                    changed += 1
            if changed:
                self._save_unlocked(data)
        if changed:
            publish_overlay_refresh("")
        return changed

    def prepare_startup(self, *, remove_game_panels: bool = True, hide_all: bool = True) -> dict[str, int]:
        """Normalize overlay lifecycle at JARVIS startup.

        Persistent user overlays (ordinary notes/checklists/music) retain their
        content, style and placement, but start hidden by default. Runtime
        game-bound panels are deleted so stale per-game HUD state cannot leak
        into a later session. ``hide_all=False`` is reserved for explicit
        maintenance/test callers that really want to preserve visibility.
        """
        with _LOCK:
            data = self._load_unlocked()
            panels = data.setdefault("panels", {})
            removed = hidden = 0
            for key in list(panels):
                panel = panels.get(key)
                if not isinstance(panel, dict):
                    continue
                purpose = str(panel.get("purpose", "") or "").casefold()
                kind = str(panel.get("kind", "text") or "text").casefold()
                binding = panel.get("binding", {}) if isinstance(panel.get("binding"), dict) else {}
                # User-authored notes/checklists are persistent data, including
                # game-specific ones. Only runtime game HUD/session panels are
                # disposable at startup; content is otherwise deleted only by an
                # explicit user command.
                is_user_persistent = kind in {"checklist", "music"} or purpose in {"notes", "game_notes", "music"}
                runtime_game = (
                    purpose == "game_session"
                    or str(binding.get("type", "") or "").casefold() == "game_session"
                    or (purpose.startswith("game_") and not is_user_persistent)
                )
                if remove_game_panels and runtime_game:
                    panels.pop(key, None)
                    removed += 1
                    continue
                if hide_all and bool(panel.get("visible", False)):
                    panel["visible"] = False
                    panel["updated_at"] = time.time()
                    hidden += 1
            if removed or hidden:
                self._save_unlocked(data)
                publish_overlay_refresh("")
            return {"removed": removed, "hidden": hidden}

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _base_panel(self, name: str, *, kind: str = "text") -> dict[str, Any]:
        configured = self.defaults.get("style", {}) if isinstance(self.defaults.get("style"), dict) else {}
        style = _default_style()
        style.update({k: v for k, v in configured.items() if v is not None})
        return {
            "name": _safe_name(name),
            "title": _safe_name(name),
            "kind": "checklist" if kind == "checklist" else ("music" if kind == "music" else "text"),
            # Semantic identity is deliberately separate from the visible name.
            # Voice commands can then refer to "чек-лист по игре" or "это окно"
            # without reconstructing a fragile synthetic filename-like name.
            "purpose": "",
            "game_id": "",
            "game_name": "",
            "aliases": [],
            "binding": None,
            "live_state": None,
            "text": "",
            "items": [],
            "visible": True,
            "monitor": max(1, int(self.defaults.get("monitor", 1) or 1)),
            "position": _normalize_position(str(self.defaults.get("position", "top_left"))),
            "offset_x": int(self.defaults.get("offset_x", 0) or 0),
            "offset_y": int(self.defaults.get("offset_y", 0) or 0),
            "style": style,
            "updated_at": time.time(),
        }

    @staticmethod
    def _normalize_items(raw: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return result
        for value in raw:
            if isinstance(value, dict):
                text = str(value.get("text", "") or "").strip()
                checked = bool(value.get("checked", False))
            else:
                text = str(value or "").strip()
                checked = False
            if text:
                result.append({"text": text[:500], "checked": checked})
        return result[:100]

    @staticmethod
    def _normalize_aliases(raw: Any) -> list[str]:
        values = raw if isinstance(raw, list) else [raw] if raw else []
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = re.sub(r"\s+", " ", str(value or "").strip())[:120]
            key = text.casefold().replace("ё", "е")
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result[:32]

    @staticmethod
    def _is_game_session_panel(panel: Any) -> bool:
        if not isinstance(panel, dict):
            return False
        purpose = str(panel.get("purpose", "") or "").casefold()
        binding = panel.get("binding", {}) if isinstance(panel.get("binding"), dict) else {}
        return purpose == "game_session" or str(binding.get("type", "") or "").casefold() == "game_session"

    @classmethod
    def _dedupe_game_session_panels(cls, panels: dict[str, Any], preferred_name: str = "") -> str:
        """Enforce the v2.8 invariant: there can be only one game-session panel.

        The visible title/name is not part of session identity.  If legacy code or
        speech variations created several session panels, keep the newest one (or
        the explicitly preferred existing one) and remove the rest atomically.
        """
        session_keys = [str(key) for key, panel in panels.items() if cls._is_game_session_panel(panel)]
        if not session_keys:
            return ""
        preferred_existing = next((key for key in session_keys if key.casefold() == str(preferred_name or "").casefold()), "")
        if preferred_existing:
            keep = preferred_existing
        else:
            keep = max(
                session_keys,
                key=lambda key: float((panels.get(key) or {}).get("updated_at", 0.0) or 0.0),
            )
        for key in list(session_keys):
            if key != keep:
                panels.pop(key, None)
        return keep

    def apply(self, args: dict[str, Any]) -> tuple[str, Any]:
        action = str(args.get("action", "show") or "show").strip().casefold()
        aliases = {
            "create": "show", "open": "show", "display": "show",
            "create_checklist": "checklist_create", "add_item": "checklist_add",
            "check_item": "checklist_check", "uncheck_item": "checklist_uncheck",
            "remove_item": "checklist_remove", "set_content": "set_text",
            "append_content": "append_text", "clear_content": "clear_text",
            "clear_checklist": "checklist_clear",
            "complete_checklist": "checklist_complete_all",
            "uncheck_all": "checklist_uncheck_all",
            "remove_note": "remove_text", "delete_note": "remove_text",
            "rename_panel": "rename", "rename_window": "rename",
        }
        action = aliases.get(action, action)
        name = _safe_name(str(args.get("name", "") or args.get("title", "") or "Информация"))

        with _LOCK:
            data = self._load_unlocked()
            panels: dict[str, Any] = data.setdefault("panels", {})

            ambiguity188: list[str] = []

            def ambiguous_reply188(spoken: str) -> tuple[str, Any]:
                names = ", ".join(f"«{n}»" for n in ambiguity188[:5])
                return (f"Под «{spoken}» подходят несколько окон: {names}. Какое из них?",
                        {"name": spoken, "found": False, "requires_clarification": True, "options": list(ambiguity188[:5])})

            def resolve_panel_key(spoken_name: str) -> str:
                """Resolve a stored panel by title *or* natural spoken alias.

                Agent-side parsing normally resolves aliases before this store is
                called, but plugins and direct tool calls can reach OverlayStore
                themselves. Keeping the same safety rule here prevents commands
                such as “закрой оверлей музыки” from creating/missing a duplicate.
                """
                def key(value: object) -> str:
                    raw = str(value or "").casefold().replace("ё", "е")
                    raw = re.sub(r"[^a-zа-я0-9]+", " ", raw, flags=re.I)
                    return re.sub(r"\s+", " ", raw).strip()

                wanted = key(spoken_name)
                ambiguity188.clear()
                if not wanted:
                    return ""
                # HIST-043: an exact name beats a title, a title beats an alias, and
                # several equal matches at the best level are a question, never
                # «the first inserted panel».
                tiers: list[list[str]] = [[], [], []]
                fuzzy: list[tuple[float, str]] = []
                for stored_key, value in panels.items():
                    if not isinstance(value, dict):
                        continue
                    aliases = value.get("aliases", []) if isinstance(value.get("aliases"), list) else []
                    if wanted in {key(stored_key), key(value.get("name", ""))} - {""}:
                        tiers[0].append(str(stored_key))
                        continue
                    if key(value.get("title", "")) == wanted:
                        tiers[1].append(str(stored_key))
                        continue
                    if wanted in {key(alias) for alias in aliases}:
                        tiers[2].append(str(stored_key))
                        continue
                    candidates = [stored_key, value.get("name", ""), value.get("title", ""), *aliases]
                    candidate_keys = [key(candidate) for candidate in candidates if key(candidate)]
                    score = 0.0
                    for candidate in candidate_keys:
                        if wanted in candidate or candidate in wanted:
                            score = max(score, min(len(wanted), len(candidate)) / max(len(wanted), len(candidate)))
                    if score >= 0.78:
                        fuzzy.append((score, str(stored_key)))
                for tier in tiers:
                    if len(tier) == 1:
                        return tier[0]
                    if len(tier) > 1:
                        ambiguity188.extend(tier)
                        return ""
                if fuzzy:
                    fuzzy.sort(reverse=True)
                    if len(fuzzy) == 1 or fuzzy[0][0] > fuzzy[1][0]:
                        return fuzzy[0][1]
                return ""

            # A game-session HUD is a singleton by design.  Its identity is the
            # semantic purpose/binding, not a spoken panel name.  This prevents
            # "Сессия", "Игровая сессия" and an old legacy HUD from coexisting
            # on different monitors and receiving only some of the commands.
            requested_binding = args.get("binding") if isinstance(args.get("binding"), dict) else {}
            requested_is_session = (
                str(args.get("purpose", "") or "").casefold() == "game_session"
                or str(requested_binding.get("type", "") or "").casefold() == "game_session"
            )
            if requested_is_session and action not in {"list", "hide_all", "delete_all", "delete"}:
                existing_session = self._dedupe_game_session_panels(panels, name)
                name = existing_session or "Игровая сессия"

            if action == "list":
                items = [deepcopy(value) for value in panels.values() if isinstance(value, dict)]
                items.sort(key=lambda item: float(item.get("updated_at", 0.0) or 0.0), reverse=True)
                return f"Панелей: {len(items)}", items

            if action in {"hide_all", "delete_all"}:
                wanted_kind = str(args.get("kind", "") or "").casefold()
                wanted_purpose = str(args.get("purpose", "") or "").casefold()
                changed = 0
                affected: list[str] = []
                for key in list(panels):
                    panel = panels.get(key)
                    if not isinstance(panel, dict):
                        continue
                    if wanted_kind and str(panel.get("kind", "") or "").casefold() != wanted_kind:
                        continue
                    purpose = str(panel.get("purpose", "") or "").casefold()
                    if wanted_purpose and not (purpose == wanted_purpose or purpose.startswith(wanted_purpose)):
                        continue
                    if action == "delete_all":
                        panels.pop(key, None)
                        changed += 1
                        affected.append(str(key))
                    elif bool(panel.get("visible", False)):
                        panel["visible"] = False
                        panel["updated_at"] = time.time()
                        changed += 1
                        affected.append(str(key))
                if changed:
                    self._save_unlocked(data)
                    publish_overlay_refresh("")
                verb = "Удалено" if action == "delete_all" else "Скрыто"
                return f"{verb} окон: {changed}", {"count": changed, "names": affected}

            if action == "delete":
                delete_key = resolve_panel_key(name) or name
                if ambiguity188:
                    return ambiguous_reply188(name)
                existed = panels.pop(delete_key, None) is not None
                name = delete_key if existed else name
                if existed:
                    self._save_unlocked(data)
                    publish_overlay_refresh(name)
                return (f"Панель «{name}» удалена" if existed else f"Панель «{name}» не найдена"), {"name": name, "deleted": existed}

            if action == "rename":
                old_key = resolve_panel_key(name)
                if ambiguity188:
                    return ambiguous_reply188(name)
                if not old_key:
                    return (
                        f"Панель «{name}» не найдена. Я не создавал новое окно вместо него.",
                        {"name": name, "found": False, "candidates": [str(key) for key in panels.keys()][:20]},
                    )
                new_name = _safe_name(str(args.get("new_name", "") or args.get("title", "") or ""))
                if not str(args.get("new_name", "") or args.get("title", "") or "").strip():
                    return "Не указано новое название окна.", {"name": old_key, "renamed": False}
                conflict = next((key for key in panels if str(key).casefold() == new_name.casefold() and key != old_key), "")
                if conflict:
                    return (
                        f"Окно «{new_name}» уже существует. Переименование отменено.",
                        {"name": old_key, "new_name": new_name, "renamed": False, "conflict": str(conflict)},
                    )
                panel = panels.pop(old_key)
                panel["name"] = new_name
                panel["title"] = new_name
                panel["updated_at"] = time.time()
                panels[new_name] = panel
                self._save_unlocked(data)
                publish_overlay_refresh(str(old_key))
                publish_overlay_refresh(new_name)
                return f"Панель «{old_key}» переименована в «{new_name}»", {
                    **deepcopy(panel), "old_name": str(old_key), "name": new_name, "renamed": True,
                }

            # Voice/LLM capitalization must not create duplicate panels (e.g.
            # “Задачи” and “задачи”). Preserve the original stored spelling while
            # resolving references case-insensitively.
            existing_key = resolve_panel_key(name)
            if ambiguity188:
                return ambiguous_reply188(name)
            if existing_key:
                name = existing_key
            panel = panels.get(name)
            requested_kind = "checklist" if action.startswith("checklist_") else str(args.get("kind", "text") or "text")
            create_actions = {"show", "checklist_create", "checklist_add", "set_text", "append_text", "style"}
            if not isinstance(panel, dict):
                if action not in create_actions:
                    candidates = [str(key) for key in panels.keys()]
                    return (
                        f"Панель «{name}» не найдена. Я не создавал новое окно вместо него.",
                        {"name": name, "found": False, "candidates": candidates[:20]},
                    )
                panel = self._base_panel(name, kind=requested_kind)
                panels[name] = panel

            if action in {"show", "checklist_create", "set_text", "append_text"}:
                panel["visible"] = True
            elif action in {"hide", "close"}:
                panel["visible"] = False

            if action == "checklist_create":
                panel["kind"] = "checklist"
                if "items" in args:
                    panel["items"] = self._normalize_items(args.get("items"))
            elif action == "set_text":
                panel["kind"] = "text" if str(args.get("kind", "text")) != "checklist" else "checklist"
                panel["text"] = str(args.get("text", "") or "")[:20000]
            elif action == "append_text":
                panel["kind"] = "text"
                addition = str(args.get("text", "") or "").strip()
                current = str(panel.get("text", "") or "").rstrip()
                panel["text"] = ((current + "\n" if current and addition else current) + addition)[:20000]
            elif action == "clear_text":
                panel["kind"] = "text"
                panel["text"] = ""
            elif action == "remove_text":
                panel["kind"] = "text"
                needle = str(args.get("text", "") or args.get("item", "") or "").strip()
                if not needle:
                    return "Не указан текст заметки для удаления.", {"name": name, "matches": 0}
                lines = str(panel.get("text", "") or "").splitlines()
                norm = needle.casefold().replace("ё", "е")
                exact = [i for i, line in enumerate(lines) if line.strip().casefold().replace("ё", "е") == norm]
                partial = [i for i, line in enumerate(lines) if norm and norm in line.strip().casefold().replace("ё", "е")]
                matches = exact or partial
                if len(matches) != 1:
                    return f"Не удалось однозначно найти заметку «{needle}» в «{name}».", {"name": name, "matches": len(matches)}
                lines.pop(matches[0])
                panel["text"] = "\n".join(lines).strip()
            elif action == "show":
                if args.get("kind") in {"text", "checklist"}:
                    panel["kind"] = args["kind"]
                if "text" in args:
                    panel["text"] = str(args.get("text", "") or "")[:20000]
                if "items" in args:
                    panel["kind"] = "checklist"
                    panel["items"] = self._normalize_items(args.get("items"))

            if "title" in args and str(args.get("title", "")).strip():
                panel["title"] = str(args["title"]).strip()[:120]

            # Semantic metadata survives every style/move/content mutation.  It is
            # what lets the voice layer address JARVIS-owned objects by purpose
            # instead of accidentally inventing names like ``Задачи_Игра``.
            for meta_key in ("purpose", "game_id", "game_name"):
                if meta_key in args and args.get(meta_key) is not None:
                    panel[meta_key] = str(args.get(meta_key) or "").strip()[:160]
            if "aliases" in args and args.get("aliases") is not None:
                panel["aliases"] = self._normalize_aliases(args.get("aliases"))
            else:
                panel.setdefault("aliases", [])
            if "binding" in args:
                raw_binding = args.get("binding")
                panel["binding"] = deepcopy(raw_binding) if isinstance(raw_binding, dict) else None
            else:
                panel.setdefault("binding", None)
            if "live_state" in args:
                raw_live = args.get("live_state")
                panel["live_state"] = deepcopy(raw_live) if isinstance(raw_live, dict) else None
            else:
                panel.setdefault("live_state", None)

            # An explicit semantic move to another monitor/anchor means "put it
            # exactly there".  Old manual-drag offsets must not leak into the new
            # command; this was why e.g. "third screen, top-center" could land far
            # away from the requested anchor.  Nudge/drag commands pass offsets
            # explicitly and therefore preserve them.
            if action == "move" and (
                ("monitor" in args and args.get("monitor") is not None)
                or ("position" in args and args.get("position"))
                or ("offset_x" in args and args.get("offset_x") is not None)
                or ("offset_y" in args and args.get("offset_y") is not None)
            ):
                # An individual panel move must remain individual.  Category-level
                # placement from Settings is only a default for panels that have
                # never been explicitly moved by voice/tool/manual drag.
                panel["placement_override"] = True
                if ("monitor" in args or "position" in args):
                    if "offset_x" not in args:
                        panel["offset_x"] = 0
                    if "offset_y" not in args:
                        panel["offset_y"] = 0

            if "monitor" in args and args.get("monitor") is not None:
                panel["monitor"] = max(1, min(16, int(args["monitor"])))
            if "position" in args and args.get("position"):
                panel["position"] = _normalize_position(str(args["position"]))
            for key in ("offset_x", "offset_y"):
                if key in args and args.get(key) is not None:
                    panel[key] = max(-5000, min(5000, int(args[key])))

            style = panel.setdefault("style", _default_style())
            style_keys = {
                "font_family", "font_size", "text_color", "background_color", "border_color", "theme_preset",
                "control_color", "control_background_color", "control_border_color", "control_background_opacity", "control_border_opacity",
                "opacity", "background_opacity", "border_opacity", "border_width", "padding", "width", "height", "click_through", "drag_enabled",
            }
            explicit_style = False
            for key in style_keys:
                if key not in args or args.get(key) is None:
                    continue
                explicit_style = True
                value = args[key]
                if key == "font_size": value = max(8, min(96, int(value)))
                elif key in {"opacity", "background_opacity", "border_opacity", "control_background_opacity", "control_border_opacity"}: value = max(0, min(100, int(value)))
                elif key == "width": value = max(160, min(1600, int(value)))
                elif key == "height":
                    # 0 restores automatic content height; explicit values stay
                    # within a sane HUD range.
                    value = int(value)
                    value = 0 if value <= 0 else max(90, min(1400, value))
                elif key == "padding": value = max(0, min(80, int(value)))
                elif key == "border_width": value = max(0, min(12, int(value)))
                elif key in {"click_through", "drag_enabled"}: value = bool(value)
                elif key in {"text_color", "background_color", "border_color", "control_color", "control_background_color", "control_border_color"}: value = _normalize_color(value)
                else: value = str(value)
                style[key] = value
                # Legacy voice/tool calls use `opacity`; keep it as a compatibility
                # alias for background alpha now that border alpha is independent.
                if key == "opacity":
                    style["background_opacity"] = value
            if explicit_style:
                # Per-panel changes made explicitly from Telegram/voice must win
                # over category defaults from Settings.  Settings remains the
                # default for panels that have never been individually styled.
                panel["style_override"] = True

            items = panel.setdefault("items", [])
            item_text = str(args.get("item", "") or "").strip()
            if action == "checklist_add":
                panel["kind"] = "checklist"; panel["visible"] = True
                if item_text:
                    items.append({"text": item_text[:500], "checked": False})
            elif action == "checklist_clear":
                panel["kind"] = "checklist"
                panel["items"] = []
            elif action in {"checklist_complete_all", "checklist_uncheck_all"}:
                panel["kind"] = "checklist"
                for item in items:
                    if isinstance(item, dict):
                        item["checked"] = action == "checklist_complete_all"
            elif action in {"checklist_check", "checklist_uncheck", "checklist_remove"}:
                panel["kind"] = "checklist"
                needle = item_text.casefold()
                exact = [i for i, value in enumerate(items) if str(value.get("text", "")).casefold() == needle]
                partial = [i for i, value in enumerate(items) if needle and needle in str(value.get("text", "")).casefold()]
                matches = exact or partial
                if len(matches) != 1:
                    return (f"Не удалось однозначно найти пункт «{item_text}» в «{name}»."), {"name": name, "matches": len(matches)}
                index = matches[0]
                if action == "checklist_remove":
                    items.pop(index)
                else:
                    items[index]["checked"] = action == "checklist_check"

            if action not in {
                "show", "hide", "close", "set_text", "append_text", "clear_text", "remove_text", "style", "move",
                "checklist_create", "checklist_add", "checklist_check", "checklist_uncheck", "checklist_remove", "checklist_clear",
                "checklist_complete_all", "checklist_uncheck_all",
            }:
                raise ValueError(f"Неизвестное действие overlay: {action}")

            panel["updated_at"] = time.time()
            self._save_unlocked(data)
            publish_overlay_refresh(name)
            snapshot = deepcopy(panel)

        if action == "checklist_add":
            return f"Добавил пункт в чек-лист «{name}»: {item_text}", snapshot
        if action == "checklist_check":
            return f"Отметил пункт в чек-листе «{name}»: {item_text}", snapshot
        if action == "checklist_uncheck":
            return f"Снял отметку с пункта «{item_text}» в «{name}»", snapshot
        if action == "checklist_remove":
            return f"Удалил пункт из чек-листа «{name}»: {item_text}", snapshot
        if action == "checklist_clear":
            return f"Очистил чек-лист «{name}»", snapshot
        if action == "checklist_complete_all":
            return f"Завершил все пункты чек-листа «{name}»", snapshot
        if action == "checklist_uncheck_all":
            return f"Снял отметки со всех пунктов чек-листа «{name}»", snapshot
        if action == "append_text":
            return f"Добавил заметку в «{name}»", snapshot
        if action == "clear_text":
            return f"Очистил текст панели «{name}»", snapshot
        if action == "remove_text":
            return f"Удалил заметку из «{name}»", snapshot
        if action in {"hide", "close"}:
            return f"Скрыл панель «{name}»", snapshot
        if action == "style":
            return f"Настройки панели «{name}» обновлены", snapshot
        if action == "move":
            return f"Панель «{name}» перемещена", snapshot
        return f"Панель «{name}» отображена", snapshot
