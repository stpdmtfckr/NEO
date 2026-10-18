# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Compatibility direct-action grammar extracted from :mod:`jarvis.agent`.

This mixin is intentionally behavior-preserving.  It is not the destination for
new commands: new deterministic functionality belongs in a domain router/service.
Keeping this large historical grammar isolated lets Agent stay an orchestrator and
allows domain-by-domain migration without a flag-day rewrite.
"""

import json
import logging
import difflib
import re
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from jarvis.agent_types import AgentTurn, PendingAction
from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.desktop_context import inspect_foreground_context
from jarvis.desktop_resolver import normalize as normalize_target, application_candidates, application_candidate_is_exact
from jarvis.integrations.intents import parse_integration_intent
from jarvis.integrations.youtube_audio import extract_youtube_url
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.runtime.objects import DesktopObject, desktop_objects
from jarvis.runtime.router import norm as router_norm
from jarvis.runtime.text_normalization import normalize_spoken_command
from jarvis.runtime.phrase_atlas import normalize_phrase_atlas
from jarvis.runtime.semantics import (
    cross_domain_conflict, explicit_window_move, explicit_video, explicit_music,
    explicit_discord_media, explicit_yandex_music, looks_like_browser_search,
    looks_like_general_question, pause_like, resume_like,
)

logger = logging.getLogger(__name__)


class LegacyDirectActionMixin:
    def _fast_direct_action(self, text: str) -> AgentTurn | None:
        """Execute common desktop language deterministically and fuzzily.

        Small local models are useful for open-ended reasoning, but they should not
        be asked to rediscover obvious intents such as "останови видео" or
        "открой Downloads и найди report.pdf". Keeping these paths deterministic
        makes the assistant faster and prevents typo/case variations from turning
        into an exact exe/path lookup.
        """

        normalized = re.sub(r"\s+", " ", text.casefold().replace("ё", "е")).strip(" .,!?:;—-")
        # Whisper may return a Russian command in Latin transliteration. Repair only
        # known command/function words; names such as Important/GitHub stay intact.
        translit = {
            "otkroj": "открой", "otkroy": "открой", "atkroy": "открой",
            "zakroj": "закрой", "zakroy": "закрой", "zapusti": "запусти",
            "najdi": "найди", "naydi": "найди", "sozdaj": "создай", "sozday": "создай",
            "zapishi": "запиши", "dobav": "добавь", "sohrani": "сохрани", "sokrani": "сохрани",
            "perejdi": "перейди", "pereydi": "перейди", "pereklyuchi": "переключи",
            "papku": "папку", "papka": "папка", "folder": "папку",
            "fajl": "файл", "fail": "файл", "file": "файл",
            "prilozhenie": "приложение", "application": "приложение", "app": "приложение",
            "igra": "игра", "game": "игра", "launcher": "launcher",
            "vkladku": "вкладку", "vkladka": "вкладка", "tab": "вкладку",
            "brauzer": "браузер", "browser": "браузер",
            "sledushchiy": "следующий", "sledushii": "следующий",
            "predydushchiy": "предыдущий", "predidushii": "предыдущий",
        }
        parts = normalized.split()
        if parts:
            normalized = " ".join(translit.get(token, token) for token in parts)

        # Whisper occasionally confuses «напиши» with «найди».  In ordinary
        # Russian «найди в этот файл 123» is not a meaningful search command,
        # while it is a very common continuation of an open-file workflow.
        # Repair only this *structurally unambiguous* form so genuine
        # «найди в файле слово X» requests keep their search meaning.
        normalized = re.sub(
            r"^найди\s+в\s+(?=(?:этот\s+)?файл\b|него\b)",
            "напиши в ",
            normalized,
            count=1,
        )

        # Voice recognition often returns an infinitive or a close colloquial
        # imperative. Canonicalize only the leading verb so the object text stays
        # untouched (file names, track names and URLs must never be rewritten).
        verb_aliases = {
            "открыть": "открой", "открывай": "открой",
            "запустить": "запусти", "запускай": "запусти",
            "создать": "создай", "сделай файл": "создай файл",
            "переместить": "перемести", "перенести": "перенеси", "перекинь": "перемести",
            "удалить": "удали", "убери в корзину": "удали",
            "сохранить": "сохрани", "закрыть": "закрой",
            "найти": "найди", "отыскать": "отыщи",
            "включить": "включи", "выключить": "выключи",
        }
        for source, target_verb in sorted(verb_aliases.items(), key=lambda item: len(item[0]), reverse=True):
            if normalized == source or normalized.startswith(source + " "):
                normalized = target_verb + normalized[len(source):]
                break

        # Contextual pronouns: resolve against the concrete last object rather than
        # asking the model to guess which Chrome/Explorer/window was meant.
        if re.search(r"\b(?:его|ее|её|это|этот|текущее|текущий)\b", normalized) or re.fullmatch(r"(?:разверни|сверни|закрой)", normalized):
            monitor_match = re.search(r"(?:на\s+)?(\d+)\s*(?:-?й|-?м)?\s*(?:экран|монитор)", normalized)
            last_obj = desktop_objects().last("window") or desktop_objects().last("application")
            title = str(getattr(last_obj, "label", "") or "") if last_obj is not None else str((getattr(self, "_last_foreground_context", {}) or {}).get("title", "") or "")
            if title and monitor_match and re.search(r"\b(?:перенеси|перемести)\w*\b", normalized):
                args={"action":"move_to_monitor","title":title,"monitor":int(monitor_match.group(1)),"kind":"any"}
                execution=self.tools.execute("window",args); self._remember_execution_context("window",args,execution)
                return AgentTurn(execution.text, tool_details=[f"window: {execution.text}"])
            if title and re.search(r"\bразверн\w*\b", normalized):
                args={"action":"maximize","title":title,"kind":"any"}; execution=self.tools.execute("window",args); self._remember_execution_context("window",args,execution)
                return AgentTurn(execution.text, tool_details=[f"window: {execution.text}"])
            if title and re.search(r"\bсверн\w*\b", normalized):
                args={"action":"minimize","title":title,"kind":"any"}; execution=self.tools.execute("window",args); self._remember_execution_context("window",args,execution)
                return AgentTurn(execution.text, tool_details=[f"window: {execution.text}"])
            if title and re.search(r"\bзакрой\w*\b", normalized) and not re.search(r"\bвкладк", normalized):
                args={"action":"close","title":title,"kind":"any"}; execution=self.tools.execute("window",args); self._remember_execution_context("window",args,execution)
                return AgentTurn(execution.text, tool_details=[f"window: {execution.text}"])
        if re.search(r"\bоткрой\s+там\s+(?:youtube|ютуб)\b", normalized):
            args={"action":"open_url","url":"https://www.youtube.com"}; execution=self.tools.execute("browser",args); self._remember_execution_context("browser",args,execution)
            return AgentTurn(execution.text, tool_details=[f"browser: {execution.text}"])

        pending_overlay = self._resolve_pending_overlay_followup(text, normalized)
        if pending_overlay is not None:
            return pending_overlay

        # ---------- HOTFIX29 explicit panel/overlay semantic firewall ----------
        # If the user literally says "панель" or "оверлей", that noun wins over
        # media/files/plugins. This prevents e.g. "включи панель музыка" from being
        # sent to the YouTube Media tool and keeps notes/checklists as JARVIS UI objects.
        panel_word = bool(re.search(r"\b(?:панел\w*|оверле\w*)\b", normalized, flags=re.I))
        if panel_word:
            category = ""
            if re.search(r"\b(?:музык\w*|music)\b", normalized, flags=re.I): category = "music"
            elif re.search(r"\b(?:чек[- ]?лист\w*|задач\w*)\b", normalized, flags=re.I): category = "checklist"
            elif re.search(r"\bзамет\w*\b", normalized, flags=re.I): category = "notes"
            elif re.search(r"\b(?:игров\w*\s+)?сесси\w*\b", normalized, flags=re.I): category = "session"

            if category:
                listing = self.tools.execute("overlay", {"action": "list"})
                rows = listing.data if getattr(listing, "success", False) and isinstance(getattr(listing, "data", None), list) else []

                def belongs(panel: dict[str, Any]) -> bool:
                    kind = str(panel.get("kind", "") or "").casefold()
                    purpose = str(panel.get("purpose", "") or "").casefold()
                    blob = " ".join([str(panel.get("name", "") or ""), str(panel.get("title", "") or ""), *[str(x) for x in panel.get("aliases", []) if str(x).strip()]]).casefold().replace("ё", "е")
                    if category == "music": return kind == "music" or purpose == "music" or "музык" in blob
                    if category == "checklist": return kind == "checklist"
                    if category == "notes": return kind == "text" and (purpose == "notes" or "note" in purpose or "замет" in blob)
                    return purpose == "game_session" or (kind == "text" and "сесси" in blob)

                candidates = [row for row in rows if isinstance(row, dict) and belongs(row)]
                # Resolve a specific title following the object noun when possible.
                explicit = re.sub(r"^.*?\b(?:панел\w*|оверле\w*)\b", "", normalized, count=1, flags=re.I).strip()
                explicit = re.sub(r"^(?:музык\w*|music|замет\w*|чек[- ]?лист\w*|задач\w*|(?:игров\w*\s+)?сесси\w*)\b", "", explicit, count=1, flags=re.I).strip(' ,.;:-')
                explicit = re.sub(r"\b(?:на|в)\s+(?:\d+|перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*)\s+(?:экран\w*|монитор\w*).*$", "", explicit, flags=re.I).strip(' ,.;:-')
                explicit = re.sub(r"\b(?:слева|справа|сверху|снизу|вверху|внизу|по\s+центру|посередине).*?$", "", explicit, flags=re.I).strip(' ,.;:-')
                explicit = re.sub(r"^(?:музык\w*|замет\w*|чек[- ]?лист\w*|игров\w*\s+сесси\w*)\s*", "", explicit, flags=re.I).strip(' ,.;:-')
                # Remove action words that may remain before/after the panel noun.
                explicit = re.sub(r"\b(?:включи|покажи|открой|выключи|скрой|закрой|удали|полностью|перемести|перетащи|перенеси|сдвинь)\w*\b", " ", explicit, flags=re.I)
                explicit = re.sub(r"\s+", " ", explicit).strip(' ,.;:-')
                if explicit:
                    key = re.sub(r"[^a-zа-я0-9]+", " ", explicit.casefold().replace("ё", "е"), flags=re.I).strip()
                    exact = []
                    for row in candidates:
                        names = [str(row.get("name", "") or ""), str(row.get("title", "") or ""), *[str(x) for x in row.get("aliases", []) if str(x).strip()]]
                        if any(re.sub(r"[^a-zа-я0-9]+", " ", n.casefold().replace("ё", "е"), flags=re.I).strip() == key for n in names):
                            exact.append(row)
                    if len(exact) == 1:
                        candidates = exact

                def panel_monitor(value: str) -> int | None:
                    m = re.search(r"(?:экран|монитор)\s*(?:номер|№)?\s*(\d+)|(?:на\s+)?(\d+)\s*(?:-?й|-?м)?\s*(?:экран|монитор)", value, re.I)
                    if m: return int(m.group(1) or m.group(2))
                    for stem, num in (("перв",1),("втор",2),("трет",3),("четвер",4),("пят",5)):
                        if re.search(rf"\b{stem}\w*\s+(?:экран|монитор)", value, re.I): return num
                    return None

                def panel_position(value: str) -> str | None:
                    v=value.casefold().replace("ё","е")
                    tests=((r"лев\w*.*верх\w*|верх\w*.*лев\w*|слева\s+(?:вверху|сверху)","top_left"),(r"прав\w*.*верх\w*|верх\w*.*прав\w*|справа\s+(?:вверху|сверху)","top_right"),(r"лев\w*.*(?:низ\w*|ниж\w*)|слева\s+(?:внизу|снизу)","bottom_left"),(r"прав\w*.*(?:низ\w*|ниж\w*)|справа\s+(?:внизу|снизу)","bottom_right"),(r"(?:сверху|вверху).*?(?:центр|посередине)|(?:центр|посередине).*?(?:сверху|вверху)","top_center"),(r"(?:снизу|внизу).*?(?:центр|посередине)|(?:центр|посередине).*?(?:снизу|внизу)","bottom_center"),(r"слева.*?(?:центр|посередине)|(?:центр|посередине).*?слева","center_left"),(r"справа.*?(?:центр|посередине)|(?:центр|посередине).*?справа","center_right"),(r"\b(?:по\s+центру|в\s+центре|центр|посередине)\b","center"))
                    for pat,pos in tests:
                        if re.search(pat,v,re.I): return pos
                    return None

                action = "show"
                if re.search(r"\b(?:удали|удалить|снеси)\w*\b", normalized, re.I): action = "delete"
                elif re.search(r"\b(?:закрой|скрой|выключи|убери)\w*\b", normalized, re.I): action = "hide"
                elif re.search(r"\b(?:перемести|перетащи|перенеси|сдвинь)\w*\b", normalized, re.I): action = "move"

                plural = bool(re.search(r"\bпанели\b", normalized, re.I))
                if action == "delete" and plural and len(candidates) > 1:
                    count=0
                    for row in candidates:
                        name=str(row.get("name","") or "")
                        if name and self.tools.execute("overlay", {"action":"delete","name":name}).success: count += 1
                    return AgentTurn(f"Удалено панелей: {count}.")

                if not candidates:
                    if action != "show":
                        labels={"music":"музыки","checklist":"чек-листов","notes":"заметок","session":"игровой сессии"}
                        return AgentTurn(f"Панель {labels[category]} сейчас не создана.")
                    if category == "session":
                        args = {"action": "show_session", "panel_name": "Игровая сессия"}
                    else:
                        defaults={
                            "music": {"name":"Музыка","title":"Музыка","kind":"music","purpose":"music","aliases":["музыка","панель музыки","музыкальный оверлей"]},
                            "checklist": {"name":"Чек-лист","title":"Чек-лист","kind":"checklist","purpose":"checklist","aliases":["чек-лист","чек лист","задачи","панель чек-листов"]},
                            "notes": {"name":"Заметки","title":"Заметки","kind":"text","purpose":"notes","aliases":["заметки","мои заметки","панель заметок"]},
                        }
                        args={"action":"show", **defaults[category]}
                elif len(candidates) == 1:
                    name=str(candidates[0].get("name","") or candidates[0].get("title","") or "")
                    args={"action":action,"name":name}
                else:
                    return AgentTurn("У меня несколько таких панелей. Назовите нужную панель точнее.")

                mon=panel_monitor(normalized); pos=panel_position(normalized)
                if action in {"show","move"}:
                    if mon is not None: args["monitor"]=mon
                    if pos: args["position"]=pos
                execution_tool = "game" if category == "session" and str(args.get("action", "")) == "show_session" else "overlay"
                execution=self.tools.execute(execution_tool,args)
                if getattr(execution,"success",False):
                    payload = getattr(execution,"data",{}) or {}
                    if isinstance(payload, dict) and isinstance(payload.get("overlay"), dict):
                        name = str(payload["overlay"].get("name") or args.get("panel_name", ""))
                    else:
                        name=str(payload.get("name",args.get("name",args.get("panel_name",""))) if isinstance(payload,dict) else args.get("name",args.get("panel_name","")))
                    if category=="notes" and name: self._last_notes_name=name
                    elif category=="checklist" and name: self._last_checklist_name=name
                    if name: self._last_overlay_name=name
                self._remember_execution_context(execution_tool,args,execution)
                return AgentTurn(execution.text, tool_details=[f"{execution_tool}: {execution.text}"])

        # ---------- v3.2 application integrations ----------
        cfg = getattr(self, "config", None)
        integration_context = {
            "active_domain": str(getattr(self, "_last_integration_target", "") or getattr(self, "_last_music_target", "") or ""),
            "discord_default_server": str(getattr(self, "_last_discord_server", "") or "") or str(cfg.get("integrations.discord.default_server", "") if cfg is not None else ""),
            "discord_music_channel": str(cfg.get("integrations.discord.music_text_channel", "") if cfg is not None else ""),
            "discord_voice_channel": str(cfg.get("integrations.discord.default_voice_channel", "") if cfg is not None else ""),
            "discord_known_servers": [
                str(item.get("name", "") or "").strip()
                for item in (cfg.get("integrations.discord.servers", []) or [])
                if isinstance(item, dict) and str(item.get("name", "") or "").strip()
            ] if cfg is not None else [],
            "wake_phrase": str(cfg.get("wake_word.phrase", "") if cfg is not None else ""),
            "wake_aliases": self._active_wake_aliases() if cfg is not None else None,
        }
        parsed_integration = parse_integration_intent(text, context=integration_context)
        if parsed_integration is not None:
            # A fresh explicit integration command supersedes a stale application
            # choice menu.  This is especially important for utterances containing
            # ordinals ("первый трек"), which are ordinary Yandex Music language.
            self._pending_target_choice = None
            arguments = {"action": parsed_integration.action, **dict(parsed_integration.arguments or {})}
            if (
                parsed_integration.domain == "discord"
                and parsed_integration.action != "music_bot_play"
                and not str(arguments.get("server", "") or "").strip()
            ):
                fallback_server = str(getattr(self, "_last_discord_server", "") or "")
                if not fallback_server and cfg is not None:
                    fallback_server = str(cfg.get("integrations.discord.default_server", "") or "")
                arguments["server"] = fallback_server
            execution = self.tools.execute(parsed_integration.domain, arguments)
            self._remember_execution_context(parsed_integration.domain, arguments, execution)
            return AgentTurn(execution.text, tool_details=[f"{parsed_integration.domain}: {execution.text}"])

        pending_choice = self._resolve_pending_target_choice(normalized)
        if pending_choice is not None:
            return pending_choice

        def media_target(value: str) -> str:
            domain = self._media_semantic_domain(value)
            cfg = getattr(self, "config", None)
            preferred_music = str(cfg.get("desktop.preferred_music_app", "yandex_music") if cfg is not None else "yandex_music")
            if domain == "music":
                if re.search(r"\b(?:вк|vk)\b|вконтакт", value):
                    return "вконтакте"
                return preferred_music
            if domain == "video":
                return "youtube"
            if re.search(r"\b(?:вк|vk)\b|вконтакт", value):
                return "вконтакте"
            if ("яндекс" in value or "yandex" in value) and ("музык" in value or "music" in value):
                return preferred_music
            if "ютуб" in value or "youtube" in value:
                return "youtube"
            remembered = str(getattr(self, "_last_media_target", "") or "")
            if remembered in {preferred_music, "youtube"}:
                return remembered
            return ""


        def requested_monitor(value: str) -> str:
            words = {"перв": "1", "втор": "2", "трет": "3", "четвер": "4", "пят": "5"}
            match = re.search(r"(?:на\s+)?(\d+)\s*(?:-?й|-?м)?\s*(?:экран|монитор)", value)
            if match:
                return match.group(1)
            for stem, number in words.items():
                if re.search(rf"\b{stem}\w*\s+(?:экран|монитор)", value):
                    return number
            return "all"

        def created_filename(raw_name: str, declared_type: str = "") -> str:
            name = str(raw_name or "").strip(' \"«»')
            # Natural variants: “Test с расширением json”, “Test расширение md”.
            ext_match = re.match(r"^(.+?)\s+(?:с\s+)?расширени\w*\s+([a-z0-9]{1,8})$", name, flags=re.I)
            if ext_match:
                name = ext_match.group(1).strip(' \"«»')
                declared_type = ext_match.group(2)
            aliases = {
                "текстовый": "txt", "текстов": "txt", "txt": "txt",
                "markdown": "md", "md": "md", "json": "json", "csv": "csv",
            }
            dtype = str(declared_type or "").casefold()
            ext = ""
            for key, value in aliases.items():
                if dtype == key or dtype.startswith(key):
                    ext = value
                    break
            from pathlib import Path as _Path
            if not _Path(name).suffix:
                name += "." + (ext or "txt")
            return name

        # ---------- v2.2 deterministic overlay/checklist workflow ----------
        # The LLM still has the generic overlay tool for free-form requests, but
        # these high-frequency voice forms are handled locally so “JARVIS, add a
        # checklist item” stays instant and reliable even while Ollama is busy.
        def overlay_position(value: str) -> str | None:
            v = value.casefold().replace("ё", "е")
            pairs = [
                (r"(?:лев\w*.*верх\w*|верх\w*.*лев\w*|слева\s+(?:вверху|сверху)|(?:вверху|сверху)\s+слева)", "top_left"),
                (r"(?:прав\w*.*верх\w*|верх\w*.*прав\w*|справа\s+(?:вверху|сверху)|(?:вверху|сверху)\s+справа)", "top_right"),
                (r"(?:лев\w*.*(?:низ\w*|ниж\w*)|ниж\w*.*лев\w*|слева\s+(?:внизу|снизу)|(?:внизу|снизу)\s+слева)", "bottom_left"),
                (r"(?:прав\w*.*(?:низ\w*|ниж\w*)|ниж\w*.*прав\w*|справа\s+(?:внизу|снизу)|(?:внизу|снизу)\s+справа)", "bottom_right"),
                (r"(?:(?:сверху|вверху)\s+(?:по\s+центру|посередине)|(?:по\s+центру|посередине)\s+(?:сверху|вверху))", "top_center"),
                (r"(?:(?:снизу|внизу)\s+(?:по\s+центру|посередине)|(?:по\s+центру|посередине)\s+(?:снизу|внизу))", "bottom_center"),
                (r"(?:слева\s+(?:по\s+центру|посередине)|(?:по\s+центру|посередине)\s+слева)", "center_left"),
                (r"(?:справа\s+(?:по\s+центру|посередине)|(?:по\s+центру|посередине)\s+справа)", "center_right"),
                (r"\b(?:по\s+центру|в\s+центре|центр|посередине)\b", "center"),
            ]
            for pattern, result in pairs:
                if re.search(pattern, v):
                    return result
            return None

        def overlay_monitor(value: str) -> int | None:
            # Accept both “третий экран” and the equally natural
            # “экран номер 3 / монитор №3”.  Monitor language is an attribute
            # of an overlay command and must never leak into a panel title.
            number_after = re.search(
                r"(?:экран|монитор|диспле)\w*\s*(?:номер\s*|№\s*)?(\d+)",
                str(value or ""), flags=re.I,
            )
            if number_after:
                try:
                    return max(1, min(16, int(number_after.group(1))))
                except (TypeError, ValueError):
                    pass
            word_after = re.search(
                r"(?:экран|монитор|диспле)\w*\s*(?:номер\s*)?(один|два|три|четыре|пять)\b",
                str(value or ""), flags=re.I,
            )
            if word_after:
                return {"один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5}.get(word_after.group(1).casefold())
            raw = requested_monitor(value)
            try:
                return int(raw) if raw != "all" else None
            except (TypeError, ValueError):
                return None

        def _parse_panel_creation(value: str) -> dict[str, Any] | None:
            """Parse a *JARVIS panel* creation command into independent slots.

            Notes/checklists are internal overlay objects, not files.  A compound
            utterance such as

              “создай панель заметок под названием 123 и расположи их на
               третьем экране слева вверху”

            has three independent pieces: object=notes, title=123 and placement.
            Older regexes treated the tail as one blob, which allowed placement
            words to become the title or the whole request to fall through to the
            filesystem router.  Keep this parser deterministic and deliberately
            narrow: an explicit “файл/документ” opts out to filesystem handling.
            """
            v = re.sub(r"\s+", " ", str(value or "").strip()).casefold().replace("ё", "е")
            if not v or re.search(r"\b(?:файл|документ)\w*\b", v, flags=re.I):
                return None

            create_verb = r"(?:создай|сделай)"
            notes_object = (
                r"(?:"
                r"(?:панел\w*|окн\w*)\s+(?:(?:для|с)\s+)?замет\w*"
                r"|заметочн\w*\s+(?:панел\w*|окн\w*)"
                r"|замет\w*(?:\s+(?:панел\w*|окн\w*))?"
                r")"
            )
            checklist_object = (
                r"(?:"
                r"(?:панел\w*|окн\w*)\s+(?:(?:для|с)\s+)?чек[- ]?лист\w*"
                r"|чек[- ]?лист\w*(?:\s+(?:панел\w*|окн\w*))?"
                r"|(?:панел\w*|окн\w*)\s+(?:(?:для|с)\s+)?списк\w*\s+задач\w*"
                r"|список\w*\s+задач\w*(?:\s+(?:панел\w*|окн\w*))?"
                r")"
            )
            m = re.match(
                rf"^{create_verb}\s+(?:мне\s+)?(?:нов\w*\s+)?(?P<object>{notes_object}|{checklist_object})(?P<tail>.*)$",
                v, flags=re.I,
            )
            if not m:
                return None

            object_text = str(m.group("object") or "")
            kind = "notes" if re.search(r"замет", object_text, flags=re.I) else "checklist"
            tail = str(m.group("tail") or "").strip(' \"«»,:;-')

            # Explicit title markers are semantic delimiters, not part of the name.
            tail = re.sub(
                r"^(?:под\s+названи\w*|с\s+названи\w*|под\s+имен\w*|с\s+имен\w*|назови(?:\s+(?:ее|её|его|их))?|название|имя)\s*[:=-]?\s*",
                "", tail, count=1, flags=re.I,
            ).strip(' \"«»,:;-')

            # Everything from a placement instruction onward belongs to placement,
            # not to the panel title.  Support both “и расположи их ...” and the
            # compact “... на третьем экране слева сверху”.
            boundary_patterns = [
                r"(?:^|\s+)(?:и\s+)?(?:расположи|размести|помести|поставь|перенеси|перемести)\w*\b",
                r"(?:^|\s+)(?:и\s+)?(?:на|в)\s+(?:перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|\d+)\s+(?:экране?|мониторе?|дисплее?)\b",
                r"(?:^|\s+)(?:и\s+)?(?:на|в)\s+(?:экране?|мониторе?|дисплее?)\s*(?:номер\s*|№\s*)?\d+\b",
                r"(?:^|\s+)(?:и\s+)?(?:слева|справа|сверху|снизу|вверху|внизу|посередине|по\s+центру)\b",
            ]
            boundary = len(tail)
            for pattern in boundary_patterns:
                found = re.search(pattern, tail, flags=re.I)
                if found:
                    boundary = min(boundary, found.start())
            title = tail[:boundary].strip(' \"«»,:;-')

            # If placement starts immediately, there simply is no explicit title.
            # Remove leftover conversational glue/pronouns from empty-name forms.
            title = re.sub(r"^(?:и\s+)?(?:их|его|ее|её|это|эту|панель|окно)\s*$", "", title, flags=re.I).strip()
            if title.casefold() in {"", "новые", "новый", "мои", "мой", "панель", "окно"}:
                title = ""
            if re.fullmatch(r"(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*|игров\w*", title, flags=re.I):
                title = ""

            result: dict[str, Any] = {"kind": kind, "title": title}
            mon = overlay_monitor(v)
            pos = overlay_position(v)
            if mon is not None:
                result["monitor"] = mon
            if pos:
                result["position"] = pos
            return result

        def _overlay_inventory() -> list[dict[str, Any]]:
            try:
                result = self.tools.execute("overlay", {"action": "list"})
                return result.data if result.success and isinstance(result.data, list) else []
            except Exception:
                return []

        def _overlay_ref_key(value: str) -> str:
            # STT may render the same spoken numeric title as 123, “1 2 3”,
            # “1,2,3”, “1-2-3” or “один два три”. normalize_target() turns
            # punctuation into separators. If the *whole* title is a sequence of
            # spoken digit words, canonicalise it to the same digit string; normal
            # word titles keep their token boundaries.
            value = normalize_target(str(value or ""))
            tokens = value.split()
            digit_words = {
                "ноль": "0", "нуль": "0", "один": "1", "одна": "1", "раз": "1",
                "два": "2", "две": "2", "три": "3", "четыре": "4", "пять": "5",
                "шесть": "6", "семь": "7", "восемь": "8", "девять": "9",
            }
            if tokens and all(token in digit_words for token in tokens):
                return "".join(digit_words[token] for token in tokens)
            return re.sub(r"(?<=\d)\s+(?=\d)", "", value)

        def _existing_overlay_name(explicit: str, *, kind: str = "") -> str:
            key = _overlay_ref_key(explicit)
            if not key:
                return ""
            wanted_kind = str(kind or "").casefold()
            matches: list[str] = []
            for panel in _overlay_inventory():
                if not isinstance(panel, dict):
                    continue
                if wanted_kind and str(panel.get("kind", "") or "").casefold() != wanted_kind:
                    continue
                names = [str(panel.get("name", "") or ""), *[str(x) for x in panel.get("aliases", []) if str(x).strip()]]
                if any(_overlay_ref_key(candidate) == key for candidate in names):
                    matches.append(str(panel.get("name", "") or ""))
            unique = [name for i, name in enumerate(matches) if name and name not in matches[:i]]
            return unique[0] if len(unique) == 1 else ""

        def _split_named_note_reference(value: str) -> tuple[str, str] | None:
            """Split ``<existing note name> <new text>`` using live inventory.

            This removes the ambiguity in phrases like “добавь заметку 123 найти
            пласталь”: if a notes panel named 123 exists, 123 is the target and the
            remainder is content. No filename resolver or guesswork is involved.
            """
            words = str(value or "").strip(' "«».,;:-').split()
            if not words:
                return None
            candidates: list[tuple[str, set[str]]] = []
            remembered = str(getattr(self, "_last_notes_name", "") or "")
            for panel in _overlay_inventory():
                if not isinstance(panel, dict) or str(panel.get("kind", "") or "").casefold() != "text":
                    continue
                purpose = _inferred_panel_purpose(panel)
                aliases = [str(x) for x in panel.get("aliases", []) if str(x).strip()]
                is_notes = purpose in {"notes", "game_notes"} or any("замет" in normalize_target(x) for x in aliases)
                if not is_notes and remembered and _overlay_ref_key(str(panel.get("name", ""))) == _overlay_ref_key(remembered):
                    is_notes = True
                if not is_notes:
                    continue
                name = str(panel.get("name", "") or "").strip()
                if not name:
                    continue
                keys = {_overlay_ref_key(name), *[_overlay_ref_key(x) for x in aliases]}
                candidates.append((name, {key for key in keys if key}))
            best: tuple[int, str, str] | None = None
            for count in range(1, len(words) + 1):
                prefix = " ".join(words[:count])
                key = _overlay_ref_key(prefix)
                for name, keys in candidates:
                    if key in keys:
                        remainder = " ".join(words[count:]).strip(' "«».,;:-')
                        score = count * 1000 + len(key)
                        if best is None or score > best[0]:
                            best = (score, name, remainder)
            return (best[1], best[2]) if best else None

        def _is_generic_overlay_reference(value: str) -> bool:
            v = normalize_target(str(value or ""))
            if not v:
                return True
            generic = {
                "мой", "мои", "моего", "текущий", "текущие", "этот", "эта", "этом",
                "для игры", "для моей игры", "для текущей игры", "по игре", "по моей игре",
                "по текущей игре", "игры", "игра", "в игре", "в этой игре", "игровой", "игровая",
                "для нее", "для него", "его", "ее", "её", "это", "эту", "этого", "него", "ней",
                "это окно", "этого окна", "этом окне", "эту панель", "этой панели", "в этой панели",
                "для чек листа", "для чек-листа", "для заметок", "для панели", "для окна", "для оверлея",
                "чек лист по игре", "чек-лист по игре", "задачи по игре", "заметки по игре",
            }
            if v in generic:
                return True
            # Common grammatical wrappers are semantic references, not names.
            return bool(re.fullmatch(
                r"(?:(?:в|для|по)\s+)?(?:мо(?:ем|ём|ей|ю)|текущ\w*|эт\w*)?\s*"
                r"(?:окн\w*|панел\w*|оверле\w*|чек\s*лист\w*|заметк\w*|задач\w*|игр\w*)",
                v,
            ))

        def _active_game_identity() -> tuple[str, str]:
            try:
                execution = self.tools.execute("game", {"action": "status"})
                data = execution.data if execution.success and isinstance(execution.data, dict) else {}
                return str(data.get("game_id") or ""), str(data.get("game_name") or "")
            except Exception:
                return "", ""

        def _inferred_panel_purpose(panel: dict[str, Any]) -> str:
            purpose = str(panel.get("purpose", "") or "").casefold()
            if purpose:
                return purpose
            name = normalize_target(str(panel.get("name", "") or ""))
            kind = str(panel.get("kind", "") or "").casefold()
            binding = panel.get("binding", {}) if isinstance(panel.get("binding"), dict) else {}
            if str(binding.get("type", "") or "").casefold() == "game_session":
                return "game_session"

            # Legacy v2.2-v2.4 panels did not always carry `purpose`.  Infer a
            # game role only when there is actual game evidence; a normal panel
            # named “Рабочие заметки” or “Задачи” must stay a desktop object.
            has_game_meta = bool(str(panel.get("game_id", "") or "").strip() or str(panel.get("game_name", "") or "").strip())
            gameish_name = bool(re.search(
                r"(?:darktide|elden|slay|spire|warcraft|\bwow\b|игр\w*|задачи[_ -]?игра|заметки[_ -]?игра)",
                name, flags=re.I,
            ))
            legacy_game = has_game_meta or gameish_name
            if kind == "checklist" and legacy_game and ("задач" in name or "чек лист" in name):
                return "game_checklist"
            if kind == "text" and legacy_game and "замет" in name:
                return "game_notes"
            if kind == "text" and "сесси" in name:
                return "game_session"
            return ""

        def _overlay_purpose_from_reference(value: str = "", *, kind: str = "", object_word: str = "") -> str:
            """Infer a stable semantic panel role from a spoken reference.

            Object words (checklist/notes/session/build) and phrases like
            "по игре" describe *what* the panel is, not its literal title.
            Keeping this logic in one place prevents style/move/show commands
            from accidentally targeting a similarly named desktop panel.
            """
            blob = " ".join([str(value or ""), str(object_word or "")]).casefold().replace("ё", "е")
            if re.search(r"\b(?:музык\w*|music)\b", blob, flags=re.I):
                return "music"
            is_game_ref = bool(re.search(
                r"\b(?:игров\w*|(?:по|для|в)\s+(?:мо(?:ей|ю)|текущ\w*|эт\w*)?\s*игр\w*)\b",
                blob, flags=re.I,
            ))
            if not is_game_ref:
                return ""
            # Since v2.7 notes/checklists are universal desktop objects even
            # when the user happens to use them while playing.  Only the session
            # HUD remains game-scoped.
            if re.search(r"сесси", blob, flags=re.I):
                return "game_session"
            if re.search(r"замет", blob, flags=re.I):
                return "notes"
            return ""

        def resolve_overlay_name(explicit: str = "", *, kind: str = "", purpose: str = "") -> str:
            raw = str(explicit or "").strip(' "«»,:;-')
            panels = _overlay_inventory()
            wanted_kind = str(kind or "").casefold()
            wanted_purpose = str(purpose or "").casefold()
            eligible = [p for p in panels if isinstance(p, dict) and (not wanted_kind or str(p.get("kind", "")).casefold() == wanted_kind)]

            game_id = game_name = ""
            if wanted_purpose.startswith("game_") or (raw and re.search(r"\b(?:по|для|в)\s+(?:моей\s+|текущей\s+)?игр\w*\b", raw, flags=re.I)):
                game_id, game_name = _active_game_identity()

            # Explicit real names and aliases beat conversational memory.
            if raw and not _is_generic_overlay_reference(raw):
                nr = _overlay_ref_key(raw)
                exact: list[dict[str, Any]] = []
                for panel in eligible:
                    names = [str(panel.get("name", "") or ""), *[str(x) for x in panel.get("aliases", []) if x]]
                    if any(_overlay_ref_key(name) == nr for name in names):
                        exact.append(panel)
                if exact:
                    return str(exact[0].get("name") or raw)
                scored: list[tuple[float, str]] = []
                for panel in eligible:
                    names = [str(panel.get("name", "") or ""), *[str(x) for x in panel.get("aliases", []) if x]]
                    score = 0.0
                    for candidate in names:
                        nn = _overlay_ref_key(candidate)
                        if not nn:
                            continue
                        score = max(score, 0.95 if nr and (nr in nn or nn in nr) else difflib.SequenceMatcher(None, nr, nn).ratio())
                    scored.append((score, str(panel.get("name", "") or raw)))
                scored.sort(reverse=True)
                if scored and scored[0][0] >= 0.78:
                    return scored[0][1]
                return raw

            # Semantic game objects have stable identity regardless of their title.
            if wanted_purpose:
                semantic = [p for p in eligible if _inferred_panel_purpose(p) == wanted_purpose]
                if game_id:
                    same_game = [p for p in semantic if not str(p.get("game_id") or "") or str(p.get("game_id") or "") == game_id]
                    if same_game:
                        semantic = same_game
                visible = [p for p in semantic if bool(p.get("visible", False))]
                if visible:
                    return str(visible[0].get("name") or "Информация")
                if semantic:
                    return str(semantic[0].get("name") or "Информация")

            remembered = str(getattr(self, "_last_checklist_name", "") or "") if wanted_kind == "checklist" else str(getattr(self, "_last_notes_name", "") or "") if wanted_kind == "text" else str(getattr(self, "_last_overlay_name", "") or "")
            if not remembered:
                remembered = str(getattr(self, "_last_overlay_name", "") or "")
            if remembered:
                for panel in eligible:
                    if _overlay_ref_key(str(panel.get("name", ""))) == _overlay_ref_key(remembered):
                        return str(panel.get("name") or remembered)
                # Test doubles and freshly-created windows can be remembered before
                # the next inventory refresh is observable.  Preserve conversational
                # continuity instead of inventing a new generic window name.
                if not eligible:
                    return remembered
            visible = [p for p in eligible if bool(p.get("visible", False))]
            if visible:
                return str(visible[0].get("name") or "Информация")
            if eligible:
                return str(eligible[0].get("name") or "Информация")

            if wanted_purpose == "game_session":
                return "Игровая сессия"
            return "Чек-лист" if wanted_kind == "checklist" else "Заметки" if wanted_kind == "text" else "Информация"

        def run_overlay(args: dict[str, Any]) -> AgentTurn:
            repair186 = getattr(self, "_repair186", None)
            if callable(repair186):
                args = dict(args)
                repair186("overlay", args, text)
            execution = self.tools.execute("overlay", args)
            # Adding to an explicitly requested checklist is safe to recover from
            # a missing target by creating the one generic checklist. This keeps
            # “добавь в чек-лист ...” useful on a fresh start without inventing
            # a filesystem target or a game-specific object.
            data = execution.data if isinstance(getattr(execution, "data", None), dict) else {}
            if (
                str(args.get("action", "") or "").casefold() == "checklist_add"
                and not execution.success
                and data.get("found") is False
            ):
                name = str(args.get("name", "") or "Чек-лист").strip() or "Чек-лист"
                self.tools.execute("overlay", {
                    "action": "checklist_create", "name": name, "title": name, "items": [],
                    "aliases": ["чек-лист", "задачи"],
                })
                execution = self.tools.execute("overlay", args)
            if execution.success and str(args.get("name", "") or "").strip():
                action = str(args.get("action", "") or "").casefold()
                returned = execution.data if isinstance(getattr(execution, "data", None), dict) else {}
                name = str(returned.get("name") or args.get("new_name") or args["name"]).strip()
                self._last_overlay_name = name
                returned_kind = str(returned.get("kind", "") or "").casefold()
                if action.startswith("checklist_") or str(args.get("kind", "")).casefold() == "checklist" or returned_kind == "checklist":
                    self._last_checklist_name = name
                if "замет" in name.casefold() or (action in {"append_text", "set_text", "show"} and str(args.get("purpose", "")).casefold() in {"notes", "game_notes"}):
                    self._last_notes_name = name
            self._remember_execution_context("overlay", args, execution)
            return AgentTurn(execution.text, tool_details=[f"overlay: {execution.text}"])

        # ---------- v2.7 universal JARVIS panel lifecycle ----------
        # Broad, safe commands for JARVIS-owned overlays are resolved before
        # Windows/process routing.  "Закрой информацию по сессии" must never be
        # interpreted as a request to find/close an executable window.
        if re.search(r"\b(?:закрой|скрой|спрячь|убери)\w*\b", normalized, flags=re.I):
            if re.search(r"\b(?:сесси\w*|информац\w*\s+(?:по|о)\s+сесси\w*)\b", normalized, flags=re.I):
                name = resolve_overlay_name("сессия", kind="text", purpose="game_session")
                return run_overlay({"action": "hide", "name": name})
            if re.search(r"\bвсе\b", normalized, flags=re.I) and re.search(
                r"\b(?:игров\w*|связан\w*\s+с\s+игр\w*)\b", normalized, flags=re.I
            ) and re.search(r"\b(?:окн\w*|панел\w*|таблиц\w*|информац\w*)\b", normalized, flags=re.I):
                execution = self.tools.execute("overlay", {"action": "hide_all", "purpose": "game_"})
                self._last_overlay_name = ""
                self._remember_execution_context("overlay", {"action": "hide_all", "purpose": "game_"}, execution)
                return AgentTurn(execution.text, tool_details=[f"overlay: {execution.text}"])
            if re.search(r"\bвсе\b", normalized, flags=re.I) and re.search(
                r"\b(?:дополнительн\w*\s+окн\w*|панел\w*\s+джарвис\w*|окн\w*\s+джарвис\w*|оверле\w*)\b",
                normalized, flags=re.I,
            ):
                execution = self.tools.execute("overlay", {"action": "hide_all"})
                self._last_overlay_name = self._last_checklist_name = self._last_notes_name = ""
                self._remember_execution_context("overlay", {"action": "hide_all"}, execution)
                return AgentTurn(execution.text, tool_details=[f"overlay: {execution.text}"])

        # v2.8: panel appearance is intentionally settings-only.  Voice remains
        # responsible for semantic actions (show/hide/move/content), while font,
        # colors, opacity and geometry are consistent per panel type in Settings.
        # This prevents one of several checklists/notes from accidentally receiving
        # a different visual style and eliminates fragile style regex routing.
        if (
            re.match(r"^(?:измени|поменяй|поставь|сделай|увеличь|уменьши)\b", normalized, flags=re.I)
            and re.search(
                r"(?:шрифт|цвет|фон|рамк|прозрачност|ширин|высот|размер\s+(?:окн|панел|информац|текст))",
                normalized, flags=re.I,
            )
            and re.search(r"(?:окн|панел|чек[- ]?лист|замет|сесси|информац)", normalized, flags=re.I)
        ):
            return AgentTurn(
                "Внешний вид панелей теперь настраивается в «Настройки → Компьютер → Оформление». "
                "Голосом я могу переместить, показать, скрыть или изменить содержимое панели."
            )

        # ---------- v3.3 notes/checklist identity firewall ----------
        # Panels are JARVIS-owned objects, not files and not ChatGPT UI panels.
        # Resolve rename/delete by live overlay inventory before generic language
        # handling. This also normalizes spoken numeric names such as 123/1 2 3/
        # 1,2,3/1-2-3 without changing the stored display title.
        if not re.search(r"\b(?:файл|документ)\w*\b", normalized, flags=re.I):
            # Current-panel rename: “замени название заметок на 1.45”.  The user
            # intentionally names the object type but may omit the old panel name.
            # Resolve the last matching panel, or the only matching live panel.
            # Never leak this phrase into the filesystem router.
            current_panel_rename = re.match(
                r"^(?:замени|смени|измени|поменяй)\s+(?:названи\w*|имя)\s+(?:панел\w*\s+)?(замет\w*|чек[- ]?лист\w*)\s+(?:на|в)\s+(.+)$",
                normalized, flags=re.I,
            )
            if current_panel_rename:
                object_word = str(current_panel_rename.group(1) or "")
                new_name = str(current_panel_rename.group(2) or "").strip(' "«».,;:-')
                kind = "checklist" if "чек" in object_word.casefold() else "text"
                remembered = str(getattr(self, "_last_checklist_name", "") or "") if kind == "checklist" else str(getattr(self, "_last_notes_name", "") or "")
                old_name = _existing_overlay_name(remembered, kind=kind) if remembered else ""
                if not old_name:
                    candidates = [
                        str(panel.get("name", "") or "")
                        for panel in _overlay_inventory()
                        if isinstance(panel, dict)
                        and str(panel.get("kind", "") or "").casefold() == kind
                        and str(panel.get("name", "") or "").strip()
                    ]
                    if len(candidates) == 1:
                        old_name = candidates[0]
                if old_name and new_name:
                    turn = run_overlay({"action": "rename", "name": old_name, "new_name": new_name})
                    if kind == "checklist":
                        self._last_checklist_name = new_name
                    else:
                        self._last_notes_name = new_name
                    return turn
                if not new_name:
                    return AgentTurn("Как назвать панель?")
                return AgentTurn("У меня несколько таких панелей. Скажите название заметки или чек-листа, который нужно переименовать.")

            # Direct rename form: “переименуй заметки 1 2 3 в 456” /
            # “переименуй чек-лист один два три на новый”. This is deliberately
            # inventory-backed so the split between old and new title is not
            # guessed from arbitrary text.
            direct_rename = re.match(
                r"^(?:переименуй|переименовать)\s+(?:панел\w*\s+)?(замет\w*|чек[- ]?лист\w*)\s+(.+?)\s+(?:в|на)\s+(.+)$",
                normalized, flags=re.I,
            )
            if direct_rename:
                object_word = str(direct_rename.group(1) or "")
                old_spoken = str(direct_rename.group(2) or "").strip(' "«».,;:-')
                new_name = str(direct_rename.group(3) or "").strip(' "«».,;:-')
                kind = "checklist" if "чек" in object_word.casefold() else "text"
                old_name = _existing_overlay_name(old_spoken, kind=kind)
                if old_name and new_name:
                    turn = run_overlay({"action": "rename", "name": old_name, "new_name": new_name})
                    if kind == "checklist": self._last_checklist_name = new_name
                    else: self._last_notes_name = new_name
                    return turn

            # Named checklist edit mirrors named-note editing. Resolve the
            # longest existing checklist title prefix first, then treat only the
            # remainder as the new item. This fixes “чек-лист 1 2 3 пункт X”.
            named_check_add = re.match(
                r"^(?:добавь|внеси)\s+(?:мне\s+)?(?:в\s+)?чек[- ]?лист\w*\s+(.+)$",
                normalized, flags=re.I,
            )
            if named_check_add:
                words = str(named_check_add.group(1) or "").strip(' "«».,;:-').split()
                panels_snapshot = [
                    p for p in _overlay_inventory()
                    if isinstance(p, dict) and str(p.get("kind", "") or "").casefold() == "checklist"
                ]
                best: tuple[int, str, str] | None = None
                for count in range(1, len(words) + 1):
                    candidate_key = _overlay_ref_key(" ".join(words[:count]))
                    existing = ""
                    for panel in panels_snapshot:
                        names = [str(panel.get("name", "") or ""), *[str(x) for x in panel.get("aliases", []) if str(x).strip()]]
                        if candidate_key and any(_overlay_ref_key(name) == candidate_key for name in names):
                            existing = str(panel.get("name", "") or "")
                            break
                    if existing:
                        remainder = " ".join(words[count:]).strip(' "«».,;:-')
                        if remainder:
                            best = (count, existing, remainder)
                if best is not None:
                    item = re.sub(r"^(?:пункт|задач\w*)\s+", "", best[2], flags=re.I).strip(' "«».,;:-')
                    if item:
                        return run_overlay({"action": "checklist_add", "name": best[1], "item": item})

            named_check_add_after = re.match(
                r"^(?:в\s+)?чек[- ]?лист\w*\s+(.+?)\s+(?:добавь|внеси)\s+(?:пункт\s+)?(.+)$",
                normalized, flags=re.I,
            )
            if named_check_add_after:
                old_name = _existing_overlay_name(str(named_check_add_after.group(1) or ""), kind="checklist")
                item = str(named_check_add_after.group(2) or "").strip(' "«».,;:-')
                if old_name and item:
                    return run_overlay({"action": "checklist_add", "name": old_name, "item": item})

            rename_panel = re.search(
                r"(?:переименуй|замени|смени|измени|поменяй).*?(?:названи\w*|имя)(?:\s+(?:замет\w*|чек[- ]?лист\w*|панел\w*|окн\w*))?\s+(?:на|в)\s+(.+?)(?:\s+(?:вместо|за\s+место|заместо)\s+(.+?))?(?:$|[.!?])",
                normalized, flags=re.I,
            )
            if rename_panel and re.search(r"(?:заметк|чек[- ]?лист|панел|окн)", normalized, flags=re.I):
                new_name = str(rename_panel.group(1) or "").strip(' "«».,;:-')
                old_spoken = str(rename_panel.group(2) or "").strip(' "«».,;:-')
                # In “название на X вместо Y” a non-greedy capture is enough;
                # trim conversational object words if ASR included them.
                new_name = re.sub(r"\s+(?:вместо|за\s+место|заместо).*$", "", new_name, flags=re.I).strip(' "«».,;:-')
                kind = "checklist" if re.search(r"чек[- ]?лист", normalized, flags=re.I) else "text"
                old_name = _existing_overlay_name(old_spoken, kind=kind) if old_spoken else ""
                if not old_name:
                    remembered = str(getattr(self, "_last_checklist_name", "") or "") if kind == "checklist" else str(getattr(self, "_last_notes_name", "") or "")
                    old_name = _existing_overlay_name(remembered, kind=kind) or remembered
                if old_name and new_name:
                    turn = run_overlay({"action": "rename", "name": old_name, "new_name": new_name})
                    if kind == "checklist" and getattr(turn, "text", ""):
                        self._last_checklist_name = new_name
                    if kind == "text" and getattr(turn, "text", ""):
                        self._last_notes_name = new_name
                    return turn

            # “удали заметки 1,2,3” means delete the notes panel when 1,2,3 is
            # an existing panel name. Removing a line requires explicit wording
            # such as “из заметки 123 удали строку ...”.
            delete_named_panel = re.match(
                r"^удали\s+(?:сам(?:у|и)?\s+)?(?:панел\w*\s+)?(заметк\w*|чек[- ]?лист\w*)\s+(.+)$",
                normalized, flags=re.I,
            )
            if delete_named_panel:
                object_word = str(delete_named_panel.group(1) or "")
                spoken_name = str(delete_named_panel.group(2) or "").strip(' "«».,;:-')
                kind = "checklist" if "чек" in object_word.casefold() else "text"
                exact_name = _existing_overlay_name(spoken_name, kind=kind)
                if exact_name:
                    execution = self.tools.execute("overlay", {"action": "delete", "name": exact_name})
                    if execution.success:
                        if _overlay_ref_key(getattr(self, "_last_overlay_name", "")) == _overlay_ref_key(exact_name): self._last_overlay_name = ""
                        if kind == "checklist" and _overlay_ref_key(getattr(self, "_last_checklist_name", "")) == _overlay_ref_key(exact_name): self._last_checklist_name = ""
                        if kind == "text" and _overlay_ref_key(getattr(self, "_last_notes_name", "")) == _overlay_ref_key(exact_name): self._last_notes_name = ""
                    self._remember_execution_context("overlay", {"action": "delete", "name": exact_name}, execution)
                    return AgentTurn(execution.text, tool_details=[f"overlay: {execution.text}"])
                # Without the explicit panel/window noun, «удали заметку X»
                # naturally means remove the matching note text when X is not an
                # existing notes-panel title. This keeps panel deletion and content
                # deletion unambiguous.
                if kind == "text" and not re.search(r"\b(?:панел\w*|окн\w*|оверле\w*|полностью)\b", normalized, re.I):
                    note_panels = [p for p in _overlay_inventory() if isinstance(p, dict) and str(p.get("kind", "") or "").casefold() == "text" and _inferred_panel_purpose(p) in {"", "notes", "game_notes"}]
                    if len(note_panels) == 1 and spoken_name:
                        name = str(note_panels[0].get("name", "") or "")
                        if name:
                            return run_overlay({"action": "remove_text", "name": name, "text": spoken_name})

        # ---------- v2.9 structured notes/checklist creation ----------
        # Parse object/title/placement as separate slots *before* any generic
        # filesystem/application routing.  This is the authoritative route for
        # “панель заметок” and “панель чек-листа” phrasing.
        panel_create = _parse_panel_creation(normalized)
        if panel_create is not None:
            panel_kind = str(panel_create.get("kind") or "")
            title = str(panel_create.get("title") or "").strip()
            if panel_kind == "notes":
                name = title or "Заметки"
                args: dict[str, Any] = {
                    "action": "show", "name": name, "title": name,
                    "kind": "text", "purpose": "notes",
                    "aliases": ["заметки", "мои заметки", "панель заметок"],
                }
            else:
                name = title or "Чек-лист"
                args = {
                    "action": "checklist_create", "name": name, "title": name,
                    "items": [], "aliases": ["чек-лист", "чек лист", "задачи", "панель чек-листа"],
                }
            if panel_create.get("monitor") is not None:
                args["monitor"] = int(panel_create["monitor"])
            if panel_create.get("position"):
                args["position"] = str(panel_create["position"])
            return run_overlay(args)

        # Generic notes are first-class JARVIS objects, never filesystem paths.
        # If only one notes panel exists, resolve_overlay_name() selects it even
        # after a restart, so the user does not have to repeat its title.
        notes_create = re.match(
            r"^(?:создай|сделай|открой)\s+(?:мне\s+)?(?:новые\s+)?заметк\w*"
            r"(?:\s+(?:под\s+названи\w*|с\s+названи\w*|назови)\s+)?(.+)?$",
            normalized, flags=re.I,
        )
        if notes_create and not re.search(r"\b(?:файл|документ)\w*\b", normalized, flags=re.I):
            tail = str(notes_create.group(1) or "").strip(' "«»,:;-')
            location_match = re.search(
                r"(?:^|\s)(?:на\s+)?(?:перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|\d+)\s+(?:экране?|мониторе?)|"
                r"(?:слева|справа|сверху|снизу|посередине|по\s+центру)", tail, flags=re.I,
            )
            raw_name = tail[:location_match.start()].strip(' "«»,:;-') if location_match else tail
            raw_name = re.sub(r"^(?:под\s+названи\w*|с\s+названи\w*)\s+", "", raw_name, flags=re.I).strip()
            if re.fullmatch(r"(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*|игров\w*", raw_name, flags=re.I):
                raw_name = ""
            name = raw_name or resolve_overlay_name("", kind="text", purpose="notes")
            if name in {"Игровые заметки", "Информация"}:
                name = "Заметки"
            args = {"action": "show", "name": name, "title": name, "kind": "text", "purpose": "notes", "aliases": ["заметки", "мои заметки"]}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            return run_overlay(args)

        # “Удали из моих заметок X” is an unambiguous content edit, not a
        # request to delete the notes panel itself. Keep this natural short form
        # while reserving “удали заметки NAME” for panel deletion.
        note_remove_current = (
            re.match(r"^(?:удали|убери|вычеркни)\s+из\s+(?:моих\s+|этих\s+|текущих\s+)?замет\w*\s+(.+)$", normalized, flags=re.I)
            or re.match(r"^из\s+(?:моих\s+|этих\s+|текущих\s+)?замет\w*\s+(?:удали|убери|вычеркни)\s+(.+)$", normalized, flags=re.I)
        )
        if note_remove_current and not re.search(r"\b(?:файл|документ)\w*\b", normalized, flags=re.I):
            body = str(note_remove_current.group(1) or "").strip(' "«».,;:-')
            name = resolve_overlay_name("", kind="text", purpose="notes")
            if body:
                return run_overlay({"action": "remove_text", "name": name, "text": body})

        note_remove = (
            re.match(r"^(?:из\s+)?заметк\w*\s+(.+?)\s+(?:удали|убери|вычеркни)\s+(?:текст|запись|строк\w*|пункт)\s+(.+)$", normalized, flags=re.I)
            or re.match(r"^(?:удали|убери|вычеркни)\s+(?:текст|запись|строк\w*|пункт)\s+(.+?)\s+из\s+заметк\w*\s+(.+)$", normalized, flags=re.I)
        )
        if note_remove and not re.search(r"\b(?:файл|документ)\w*\b", normalized, flags=re.I):
            first, second = str(note_remove.group(1) or "").strip(' "«».,;:-'), str(note_remove.group(2) or "").strip(' "«».,;:-')
            # First grammar: note-name then body. Second grammar: body then name.
            if normalized.startswith(("из замет", "замет")):
                spoken_name, body = first, second
            else:
                body, spoken_name = first, second
            name = _existing_overlay_name(spoken_name, kind="text") or resolve_overlay_name(spoken_name, kind="text", purpose="notes")
            return run_overlay({"action": "remove_text", "name": name, "text": body})

        note_clear = re.match(r"^очисти\s+(?:мои\s+|эти\s+|текущие\s+)?заметк\w*$", normalized, flags=re.I)
        if note_clear:
            name = resolve_overlay_name("", kind="text", purpose="notes")
            return run_overlay({"action": "clear_text", "name": name})

        # Empty/named checklist creation without requiring the exact old phrase
        # "с пунктами".  Location words are attributes, never part of the title.
        checklist_create_simple = re.match(
            r"^(?:создай|сделай|открой|добавь)\s+(?:мне\s+)?(?:новый\s+)?чек[- ]?лист"
            r"(?:\s+(?:под\s+названи\w*|с\s+названи\w*|назови)\s+)?(.+)?$",
            normalized, flags=re.I,
        )
        if (
            checklist_create_simple
            and not re.search(r"\b(?:с\s+пунктами|из\s+пунктов)\b", normalized, flags=re.I)
            and not re.search(r"\bпункт\w*\b", normalized, flags=re.I)
        ):
            tail = str(checklist_create_simple.group(1) or "").strip(' "«»,:;-')
            location_match = re.search(
                r"(?:^|\s)(?:на\s+)?(?:перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|\d+)\s+(?:экране?|мониторе?)|"
                r"(?:слева|справа|сверху|снизу|посередине|по\s+центру)", tail, flags=re.I,
            )
            raw_name = tail[:location_match.start()].strip(' "«»,:;-') if location_match else tail
            raw_name = re.sub(r"^(?:под\s+названи\w*|с\s+названи\w*)\s+", "", raw_name, flags=re.I).strip()
            if re.fullmatch(r"(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*|игров\w*", raw_name, flags=re.I):
                raw_name = ""
            name = raw_name or "Чек-лист"
            args = {"action": "checklist_create", "name": name, "title": name, "items": []}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            return run_overlay(args)

        # ---------- v2.4 semantic game/overlay routing ----------
        # Do not require one exact sentence.  These commands are intent-based so
        # speech variants such as “information about the session of the game I am
        # playing” cannot fall into the application/file resolver.
        if (
            re.search(r"\b(?:покажи|отобрази|выведи|дай)\w*", normalized, flags=re.I)
            and re.search(r"сесси\w*", normalized, flags=re.I)
            and re.search(r"игр\w*", normalized, flags=re.I)
        ):
            panel_name = resolve_overlay_name("по игре", kind="text", purpose="game_session")
            args: dict[str, Any] = {"action": "show_session", "panel_name": panel_name}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            execution = self.tools.execute("game", args)
            if execution.success:
                payload = execution.data if isinstance(getattr(execution, "data", None), dict) else {}
                overlay_payload = payload.get("overlay", {}) if isinstance(payload.get("overlay"), dict) else {}
                self._last_overlay_name = str(overlay_payload.get("name") or panel_name)
            self._remember_execution_context("game", args, execution)
            return AgentTurn(execution.text, tool_details=[f"game: {execution.text}"])

        # Location may come before the verb: “on the third screen, center-left,
        # show notes for my game”.  Treat game notes as one persistent named object.
        if re.search(r"заметк\w*", normalized, flags=re.I) and re.search(r"\b(?:покажи|отобрази|выведи|открой)\w*", normalized, flags=re.I):
            purpose = "notes"
            name = resolve_overlay_name("", kind="text", purpose=purpose)
            args = {"action": "show", "name": name, "kind": "text", "purpose": purpose,
                    "aliases": ["заметки", "мои заметки", "заметки по игре", "игровые заметки"]}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            return run_overlay(args)

        # Explicit named note editing. Resolve the title against JARVIS' live
        # notes inventory before any generic/current-note or filesystem grammar.
        # Examples: “добавь заметку 123 текст”, “напиши в заметку 1 2 3 текст”.
        named_note_add = re.match(
            r"^(?:добавь|запиши|внеси|напиши)\s+(?:мне\s+)?(?:(?:в|из)\s+)?(?:эту\s+)?заметк\w*\s+(.+)$",
            normalized, flags=re.I,
        )
        if named_note_add and not re.search(r"\b(?:файл|документ)\w*\b", normalized, flags=re.I):
            split = _split_named_note_reference(str(named_note_add.group(1) or ""))
            if split is not None:
                name, body = split
                if not body:
                    self._set_pending_overlay_followup("append_text", name, purpose="notes")
                    return AgentTurn(f"Что именно добавить в заметку «{name}»?")
                return run_overlay({
                    "action": "append_text", "name": name, "title": name,
                    "text": body, "purpose": "notes",
                })

        named_note_add_after = re.match(
            r"^(?:в\s+)?заметк\w*\s+(.+?)\s+(?:добавь|запиши|внеси|напиши)\s+(.+)$",
            normalized, flags=re.I,
        )
        if named_note_add_after and not re.search(r"\b(?:файл|документ)\w*\b", normalized, flags=re.I):
            split = _split_named_note_reference(str(named_note_add_after.group(1) or ""))
            if split is not None and not split[1]:
                name = split[0]
                body = str(named_note_add_after.group(2) or "").strip(' "«».,;:-')
                if body:
                    return run_overlay({
                        "action": "append_text", "name": name, "title": name,
                        "text": body, "purpose": "notes",
                    })

        # Pronoun/current-object note editing: once a notes panel is shown,
        # "добавь заметку X" / "добавь в эти заметки X" must mutate that
        # JARVIS-owned object and must never fall through to filesystem editing.
        note_current_add = re.match(
            r"^(?:добавь|запиши|внеси)\s+(?:мне\s+)?(?:(?:в|из)\s+)?(?:эти\s+|мои\s+|текущие\s+)?"
            r"заметк\w*\s*(?:пункт\s+)?[:,-]?\s*(.+)$",
            normalized, flags=re.I,
        )
        if note_current_add and not re.search(r"\b(?:файл|документ)\w*\b", normalized, flags=re.I):
            body = str(note_current_add.group(1) or "").strip(' "«».,;:-')
            # A qualifier such as “по игре” identifies the semantic notes object;
            # it is never part of the note body.  This route intentionally runs
            # before filesystem editing, so “заметки” cannot be mistaken for a
            # filename.
            explicit_game_notes = bool(re.search(
                r"\b(?:игров\w*|(?:по|для)\s+(?:моей\s+|текущей\s+|нынешней\s+)?игр\w*)\b",
                normalized, flags=re.I,
            ))
            if explicit_game_notes:
                body = re.sub(
                    r"^(?:по|для)\s+(?:моей\s+|текущей\s+|нынешней\s+)?игр\w*\s*[,;:.-]*\s*",
                    "", body, flags=re.I,
                ).strip(' "«».,;:-')
                purpose = "notes"
                name = resolve_overlay_name("", kind="text", purpose=purpose)
                game_id = game_name = ""
            else:
                # Without an explicit game qualifier, conversational “эти/мои
                # заметки” follows the last notes object the user interacted with.
                remembered = str(getattr(self, "_last_notes_name", "") or "")
                panels = _overlay_inventory()
                remembered_panel = next((p for p in panels if normalize_target(str(p.get("name", ""))) == normalize_target(remembered)), None)
                purpose = _inferred_panel_purpose(remembered_panel) if isinstance(remembered_panel, dict) else "notes"
                name = resolve_overlay_name("это", kind="text", purpose=purpose)
                game_id = game_name = ""
            if not body or re.fullmatch(r"(?:(?:по|для)\s+игр\w*|заметк\w*|это|что[- ]?то)", body, flags=re.I):
                metadata = {"purpose": purpose}
                if game_id: metadata["game_id"] = game_id
                if game_name: metadata["game_name"] = game_name
                metadata["aliases"] = ["заметки", "мои заметки", "заметки по игре", "игровые заметки"]
                self._set_pending_overlay_followup("append_text", name, **metadata)
                return AgentTurn("Что именно добавить в заметки?")
            args = {"action": "append_text", "name": name, "title": name, "text": body, "purpose": purpose}
            if game_id: args["game_id"] = game_id
            if game_name: args["game_name"] = game_name
            args["aliases"] = ["заметки", "мои заметки", "заметки по игре", "игровые заметки"]
            return run_overlay(args)

        # Natural game-note forms are routed before filesystem editing.  "заметки"
        # is an internal JARVIS object here, never a filename unless the user says
        # "файл"/"документ" explicitly.
        note_add = (
            re.match(r"^(?:добавь|запиши|внеси)\s+(?:мне\s+)?(?:(?:в|из)\s+)?(?:игровые\s+)?заметк\w*(?:\s+(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*)?\s*[:,-]?\s+(.+)$", normalized, flags=re.I)
            or re.match(r"^(?:(?:в|из)\s+)?(?:игровые\s+)?заметк\w*(?:\s+(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*)?[, ]+\s*(?:добавь|запиши|внеси)\s+(.+)$", normalized, flags=re.I)
        )
        if note_add:
            body = str(note_add.group(1) or "").strip(' "«».,;:-')
            is_game_notes = bool(re.search(r"\b(?:игров\w*|(?:по|для)\s+(?:моей\s+|текущей\s+|нынешней\s+)?игр\w*)\b", normalized, flags=re.I))
            # Regex backtracking may leave a trailing semantic qualifier (for
            # example ``по игре``) inside the capture group when the user has
            # not dictated the note body yet.  That qualifier identifies the
            # target panel; it is never note content.
            body_semantic = body.casefold().replace("ё", "е").strip()
            if re.fullmatch(
                r"(?:по|для)\s+(?:моей\s+|текущей\s+|нынешней\s+)?игр\w*",
                body_semantic, flags=re.I,
            ):
                body = ""
            purpose = "notes"
            name = resolve_overlay_name("", kind="text", purpose=purpose)
            game_id = game_name = ""
            if not body or body in {"это", "что то", "что-то", "заметку"}:
                self._set_pending_overlay_followup(
                    "append_text", name, purpose=purpose, game_id=game_id, game_name=game_name,
                    aliases=["заметки по игре", "игровые заметки", "заметки"] if is_game_notes else ["заметки"],
                )
                return AgentTurn("Что именно добавить в заметки?")
            args = {"action": "append_text", "name": name, "title": name, "text": body, "purpose": purpose}
            if game_id: args["game_id"] = game_id
            if game_name: args["game_name"] = game_name
            args["aliases"] = ["заметки", "мои заметки", "заметки по игре", "игровые заметки"]
            return run_overlay(args)

        incomplete_note_add = re.fullmatch(
            r"(?:добавь|запиши|внеси)\s+(?:мне\s+)?(?:(?:в|из)\s+)?(?:игровые\s+)?заметк\w*"
            r"(?:\s+(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*)?",
            normalized, flags=re.I,
        )
        if incomplete_note_add:
            is_game_notes = bool(re.search(r"\b(?:игров\w*|(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*)\b", normalized, flags=re.I))
            purpose = "notes"
            name = resolve_overlay_name("", kind="text", purpose=purpose)
            game_id = game_name = ""
            self._set_pending_overlay_followup(
                "append_text", name, purpose=purpose, game_id=game_id, game_name=game_name,
                aliases=["заметки по игре", "игровые заметки", "заметки"] if is_game_notes else ["заметки"],
            )
            return AgentTurn("Что именно добавить в заметки?")

        # ---------- v2.2 read-only game context / game overlay ----------
        # Same voice grammar as desktop overlays: the game adapter only supplies
        # normalized content; placement and styling stay universal.
        game_session = re.match(
            r"^(?:покажи|отобрази|выведи)(?:\s+мне)?\s+(?:информац\w*|данн\w*|статистик\w*)?\s*"
            r"(?:по|о)?\s*(?:моей|текущей|нынешней)?\s*(?:игровой\s+)?сесси\w*.*$",
            normalized, flags=re.I,
        )
        if game_session:
            panel_name = resolve_overlay_name("по игре", kind="text", purpose="game_session")
            args: dict[str, Any] = {"action": "show_session", "panel_name": panel_name}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            execution = self.tools.execute("game", args)
            if execution.success:
                payload = execution.data if isinstance(getattr(execution, "data", None), dict) else {}
                overlay_payload = payload.get("overlay", {}) if isinstance(payload.get("overlay"), dict) else {}
                self._last_overlay_name = str(overlay_payload.get("name") or panel_name)
            self._remember_execution_context("game", args, execution)
            return AgentTurn(execution.text, tool_details=[f"game: {execution.text}"])

        # v2.7 deliberately removed hidden per-game build/resource telemetry.
        # A build can still be kept as an ordinary note/checklist, but JARVIS
        # will not pretend to read it from arbitrary game memory/files.
        if (
            re.search(r"\b(?:покажи|отобрази|выведи)\w*", normalized, flags=re.I)
            and re.search(r"\b(?:билд|сборк)\w*", normalized, flags=re.I)
            and re.search(r"\bигр\w*", normalized, flags=re.I)
        ):
            return AgentTurn("Автоматическое чтение билдов из игр отключено. Могу хранить ваш билд как обычную заметку или чек-лист.")

        game_status = re.match(
            r"^(?:какая|что за)\s+(?:игра|игру)\s+(?:сейчас\s+)?(?:запущена|открыта)|"
            r"^(?:сколько|как долго)\s+(?:я\s+)?(?:играю|играю\s+сейчас)",
            normalized, flags=re.I,
        )
        if game_status:
            args = {"action": "status"}
            execution = self.tools.execute("game", args)
            self._remember_execution_context("game", args, execution)
            return AgentTurn(execution.text, tool_details=[f"game: {execution.text}"])

        # Gameplay questions use one fresh screenshot plus the normalized
        # read-only adapter state.  ``game.ask`` itself requires the game process
        # to be the foreground window, so a background Steam/WoW process cannot
        # hijack an ordinary desktop question.
        # v2.7: no automatic game screenshot/hidden telemetry routing.  Game
        # questions fall through to the normal assistant unless the user invokes
        # an explicit desktop/screenshot action.

        # “чек-лист по игре” is now just a universal checklist used while
        # gaming; it has no game-specific lifetime or hidden telemetry binding.
        if (
            re.search(r"\b(?:чек[- ]?лист|задач\w*)\b", normalized, flags=re.I)
            and re.search(r"\b(?:игров\w*|(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*)\b", normalized, flags=re.I)
            and re.search(r"\b(?:покажи|отобрази|открой|создай|сделай)\w*\b", normalized, flags=re.I)
            and not re.search(r"\b(?:добавь|добавить|отметь|удали|очисти)\w*\b", normalized, flags=re.I)
            and not re.search(r"\b(?:с\s+пунктами|из\s+пунктов)\b", normalized, flags=re.I)
        ):
            name = resolve_overlay_name("", kind="checklist")
            args = {"action": "checklist_create", "name": name, "title": name, "aliases": ["чек-лист", "задачи", "чек-лист по игре"]}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            return run_overlay(args)

        # “создай чек-лист Рейд с пунктами еда, починка, фласки”
        checklist_create = re.match(
            r"^(?:создай|сделай|открой)\s+(?:мне\s+)?чек[- ]?лист"
            r"(?:\s+(.*?))?\s+(?:с\s+(?:такими(?:-то)?\s+)?пунктами|из\s+пунктов)\s*[:\-]?\s*(.+)$",
            normalized, flags=re.I,
        )
        if checklist_create:
            raw_name = str(checklist_create.group(1) or "").strip(' "«»,:;-')
            # Filler before “с пунктами” is not a useful persistent panel name.
            if raw_name.casefold() in {"", "мой", "новый", "новый мой", "такой", "такой-то"}:
                raw_name = "Чек-лист"
            raw_items = str(checklist_create.group(2) or "").strip()
            items = [x.strip(' "«».') for x in re.split(r"\s*[,;]\s*|\s+и\s+(?=[^,;]+$)", raw_items) if x.strip()]
            if not items:
                return AgentTurn("Не услышал пункты чек-листа.")
            is_game_list = bool(re.search(r"\b(?:игров\w*|(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*)\b", raw_name + " " + normalized, flags=re.I))
            if is_game_list and _is_generic_overlay_reference(raw_name):
                raw_name = resolve_overlay_name("", kind="checklist")
            args: dict[str, Any] = {"action": "checklist_create", "name": raw_name, "title": raw_name, "items": items}
            if is_game_list:
                args.update({"aliases": ["чек-лист", "задачи", "чек-лист по игре", "игровой чек-лист"]})
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            return run_overlay(args)

        checklist_add_simple = re.match(
            r"^(?:добавь|внеси|запиши)\s+(?:мне\s+)?(?:в\s+)?(?:мой\s+|этот\s+|текущий\s+)?чек[- ]?лист\s+(?:пункт\s+)?(.+)$",
            normalized, flags=re.I,
        )
        if checklist_add_simple:
            item = str(checklist_add_simple.group(1) or "").strip(' "«».,;:-')
            name = resolve_overlay_name("этот чек-лист", kind="checklist")
            if not item:
                self._set_pending_overlay_followup("checklist_add", name)
                return AgentTurn("Какой пункт добавить в чек-лист?")
            return run_overlay({"action": "checklist_add", "name": name, "item": item})

        checklist_remove_simple = re.match(
            r"^(?:удали|убери|вычеркни)\s+(?:из\s+)?(?:моего\s+|этого\s+|текущего\s+)?чек[- ]?лист\w*\s+(?:пункт\s+)?(.+)$",
            normalized, flags=re.I,
        )
        if checklist_remove_simple:
            item = str(checklist_remove_simple.group(1) or "").strip(' "«».,;:-')
            name = resolve_overlay_name("этот чек-лист", kind="checklist")
            return run_overlay({"action": "checklist_remove", "name": name, "item": item})

        # Current checklist editing is object-based, not title-based.  This
        # catches short in-game phrases such as "добавь в этот чек-лист X" and
        # "добавь пункт X в чек-лист" after the panel has already been created.
        checklist_current_add = (
            re.match(r"^(?:добавь|внеси)\s+(?:новый\s+)?(?:пункт\s+)?(.+?)\s+(?:в\s+)?(?:этот|мой|текущий)\s+чек[- ]?лист$", normalized, flags=re.I)
            or re.match(r"^(?:добавь|внеси)\s+(?:в\s+)?(?:этот|мой|текущий)\s+чек[- ]?лист\s+(?:новый\s+)?(?:пункт\s+)?(.+)$", normalized, flags=re.I)
            or re.match(r"^(?:в\s+)?(?:этот|мой|текущий)\s+чек[- ]?лист[, ]+\s*(?:добавь|добавить|внеси)\s+(?:пункт\s+)?(.+)$", normalized, flags=re.I)
        )
        if checklist_current_add:
            item = str(checklist_current_add.group(1) or "").strip(' "«».,;:-')
            name = resolve_overlay_name("этот чек-лист", kind="checklist")
            if not item:
                self._set_pending_overlay_followup("checklist_add", name)
                return AgentTurn("Какой пункт добавить в этот чек-лист?")
            return run_overlay({"action": "checklist_add", "name": name, "item": item})

        checklist_complete_all = re.match(
            r"^(?:заверши|выполни|зачеркни|отметь\s+выполненным)\s+(?:весь\s+|полностью\s+)?(?:этот\s+|мой\s+|игровой\s+)?чек[- ]?лист(?:\s+(.+))?$",
            normalized, flags=re.I,
        )
        if checklist_complete_all:
            explicit = str(checklist_complete_all.group(1) or "")
            purpose = ""
            name = resolve_overlay_name(explicit or "этот чек-лист", kind="checklist", purpose=purpose)
            return run_overlay({"action": "checklist_complete_all", "name": name})

        checklist_uncheck_all = re.match(
            r"^(?:сними|убери)\s+(?:все\s+)?отметк\w*\s+(?:с\s+)?(?:этого\s+|моего\s+|игрового\s+)?чек[- ]?лист\w*(?:\s+(.+))?$",
            normalized, flags=re.I,
        )
        if checklist_uncheck_all:
            explicit = str(checklist_uncheck_all.group(1) or "")
            purpose = ""
            name = resolve_overlay_name(explicit or "этот чек-лист", kind="checklist", purpose=purpose)
            return run_overlay({"action": "checklist_uncheck_all", "name": name})

        # Live checklist editing. Accept both natural word orders:
        # “добавь [в] чек-лист пункт X” and “в чек-лист для игры добавь пункт X”.
        checklist_add = (
            re.match(r"^(?:добавь|внеси)\s+(?:мне\s+)?(?:в\s+)?(?:мой\s+)?чек[- ]?лист(?:\s+((?:(?:по|для)\s+)?(?:моей\s+|текущей\s+)?игр\w*|[^,]*?))?\s+(?:новый\s+)?пункт\s+(.+)$", normalized, flags=re.I)
            or re.match(r"^(?:в\s+)?(?:мой\s+)?чек[- ]?лист(?:\s+((?:(?:по|для)\s+)?(?:моей\s+|текущей\s+)?игр\w*|.*?))?[, ]+\s*(?:тебе\s+нужно\s+)?(?:добавь|добавить|внеси)\s+(?:новый\s+)?(?:пункт\s+)?(.+)$", normalized, flags=re.I)
            or re.match(r"^(?:добавь|внеси)\s+(?:в\s+)?(?:игровые\s+)?задач\w*\s+(?:пункт\s+)?(.+)$", normalized, flags=re.I)
        )
        if checklist_add:
            groups = checklist_add.groups()
            if len(groups) == 1:
                explicit, item = "по игре", str(groups[0] or "")
            else:
                explicit, item = str(groups[0] or ""), str(groups[1] or "")
            item = item.strip(' "«».,;:-')
            is_game_list = bool(re.search(r"\b(?:игров\w*|(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*)\b", explicit + " " + normalized, flags=re.I))
            purpose = ""
            name = resolve_overlay_name(explicit, kind="checklist", purpose=purpose)
            game_id = game_name = ""
            if not item or re.fullmatch(r"(?:пункт|что то|что-то|это)", item, flags=re.I):
                self._set_pending_overlay_followup(
                    "checklist_add", name, purpose=purpose, game_id=game_id, game_name=game_name,
                    aliases=["чек-лист", "задачи"],
                )
                return AgentTurn("Какой пункт добавить в чек-лист?")
            args = {"action": "checklist_add", "name": name, "item": item}
            if purpose:
                args.update({"purpose": purpose, "aliases": ["чек-лист по игре", "игровой чек-лист", "задачи по игре", "игровые задачи"]})
                if game_id: args["game_id"] = game_id
                if game_name: args["game_name"] = game_name
                # checklist_add does not normally auto-create missing targets.  A
                # semantic game checklist is safe to create because its identity
                # is explicit in the user's command, not guessed from a filename.
                inv = _overlay_inventory()
                if not any(_overlay_ref_key(str(p.get("name", ""))) == _overlay_ref_key(name) for p in inv if isinstance(p, dict)):
                    run_overlay({"action": "checklist_create", "name": name, "title": name, "items": [], **{k:v for k,v in args.items() if k in {"purpose","aliases","game_id","game_name"}}})
            return run_overlay(args)

        if (
            re.search(r"\bчек[- ]?лист\w*\b", normalized, flags=re.I)
            and re.search(r"\b(?:по|для)\s+(?:моей\s+|текущей\s+)?игр\w*\b|\bигров\w*\b", normalized, flags=re.I)
            and re.search(r"\b(?:добавь|добавить|внеси)\w*\b", normalized, flags=re.I)
        ):
            name = resolve_overlay_name("", kind="checklist")
            self._set_pending_overlay_followup("checklist_add", name, aliases=["чек-лист", "задачи"])
            return AgentTurn("Какой пункт добавить в чек-лист?")

        checklist_mark = re.match(
            r"^(?:отметь|выполни|заверши|зачеркни)\s+(?:в\s+)?(?:моем\s+|моём\s+)?чек[- ]?листе?(?:\s+(.*?))?\s+пункт\s+(.+)$",
            normalized, flags=re.I,
        )
        if checklist_mark:
            explicit = str(checklist_mark.group(1) or "")
            purpose = ""
            name = resolve_overlay_name(explicit, kind="checklist", purpose=purpose)
            return run_overlay({"action": "checklist_check", "name": name, "item": checklist_mark.group(2).strip()})

        checklist_mark_short = re.match(r"^(?:отметь|выполни|заверши|зачеркни)\s+(?:пункт\s+)?(.+)$", normalized, flags=re.I)
        if checklist_mark_short:
            item = str(checklist_mark_short.group(1) or "").strip(' "«».,;:-')
            # Avoid stealing broad overlay/window commands from later routers.
            if item and not re.search(r"\b(?:окн|панел|сесси)\w*\b", item, flags=re.I):
                checklists = [p for p in _overlay_inventory() if str(p.get("kind", "")).casefold() == "checklist"]
                if getattr(self, "_last_checklist_name", "") or len(checklists) == 1:
                    name = resolve_overlay_name("этот чек-лист", kind="checklist")
                    return run_overlay({"action": "checklist_check", "name": name, "item": item})
                if len(checklists) > 1:
                    names = ", ".join(f"«{str(p.get('name') or 'Чек-лист')}»" for p in checklists[:5])
                    return AgentTurn(f"У вас несколько чек-листов: {names}. Назовите, в каком отметить пункт «{item}».")

        checklist_unmark = re.match(
            r"^(?:сними\s+отметк\w*|убери\s+отметк\w*|отмени\s+выполнени\w*)\s+"
            r"(?:в\s+)?(?:чек[- ]?листе?)?(?:\s+(.*?))?\s*(?:с\s+пункта|пункт)\s+(.+)$",
            normalized, flags=re.I,
        )
        if checklist_unmark:
            explicit = str(checklist_unmark.group(1) or "")
            purpose = ""
            name = resolve_overlay_name(explicit, kind="checklist", purpose=purpose)
            return run_overlay({"action": "checklist_uncheck", "name": name, "item": checklist_unmark.group(2).strip()})

        checklist_remove = re.match(
            r"^удали\s+(?:из\s+)?чек[- ]?лист(?:а|е)?(?:\s+(.*?))?\s+пункт\s+(.+)$",
            normalized, flags=re.I,
        )
        if checklist_remove:
            explicit = str(checklist_remove.group(1) or "")
            purpose = ""
            name = resolve_overlay_name(explicit, kind="checklist", purpose=purpose)
            return run_overlay({"action": "checklist_remove", "name": name, "item": checklist_remove.group(2).strip()})

        checklist_clear = re.match(r"^очисти\s+чек[- ]?лист(?:\s+(.+))?$", normalized, flags=re.I)
        if checklist_clear:
            explicit = str(checklist_clear.group(1) or "")
            purpose = ""
            name = resolve_overlay_name(explicit, kind="checklist", purpose=purpose)
            return run_overlay({"action": "checklist_create", "name": name, "title": name, "items": []})

        checklist_delete = re.match(r"^удали\s+(?:сам\s+)?чек[- ]?лист(?:\s+(.+))?$", normalized, flags=re.I)
        if checklist_delete:
            explicit = str(checklist_delete.group(1) or "")
            purpose = ""
            name = resolve_overlay_name(explicit, kind="checklist", purpose=purpose)
            execution = self.tools.execute("overlay", {"action": "delete", "name": name})
            if execution.success and self._last_overlay_name.casefold() == name.casefold():
                self._last_overlay_name = ""
            self._remember_execution_context("overlay", {"action": "delete", "name": name}, execution)
            return AgentTurn(execution.text, tool_details=[f"overlay: {execution.text}"])

        # Show/hide/move/style existing named overlays.  Object words such as
        # “чек-лист/заметки/панель” are semantic types, not part of the panel name.
        overlay_delete_full = re.match(
            r"^(?:полностью\s+)?(?:закрой|удали)\s+(?:полностью\s+)?(?:окно|панель|оверлей|чек[- ]?лист|заметки?)?(?:\s+(.+))?$",
            normalized, flags=re.I,
        )
        if overlay_delete_full and ("полностью" in normalized.casefold() or normalized.casefold().startswith("удали окно") or normalized.casefold().startswith("удали панель") or normalized.casefold().startswith("удали оверлей")):
            raw_name = str(overlay_delete_full.group(1) or "").strip(' "«»,:;-')
            kind = "checklist" if "чек" in normalized.casefold() else "text" if "замет" in normalized.casefold() else ""
            purpose = _overlay_purpose_from_reference(raw_name, kind=kind, object_word=normalized)
            name = resolve_overlay_name(raw_name, kind=kind, purpose=purpose)
            execution = self.tools.execute("overlay", {"action": "delete", "name": name})
            if execution.success:
                if normalize_target(self._last_overlay_name) == normalize_target(name): self._last_overlay_name = ""
                if normalize_target(getattr(self, "_last_checklist_name", "")) == normalize_target(name): self._last_checklist_name = ""
                if normalize_target(getattr(self, "_last_notes_name", "")) == normalize_target(name): self._last_notes_name = ""
            self._remember_execution_context("overlay", {"action": "delete", "name": name}, execution)
            return AgentTurn(execution.text, tool_details=[f"overlay: {execution.text}"])

        overlay_pronoun_visibility = re.match(
            r"^(покажи|отобрази|скрой|спрячь|закрой)\s+(?:мне\s+)?(?:это|данное|текущее)\s+(окно|панель|оверлей)(?:\s+полностью)?$",
            normalized, flags=re.I,
        )
        if overlay_pronoun_visibility:
            verb = overlay_pronoun_visibility.group(1).casefold()
            name = resolve_overlay_name("это окно")
            action = "hide" if verb in {"скрой", "спрячь", "закрой"} else "show"
            return run_overlay({"action": action, "name": name})

        # Alpha-Beta: a saved JARVIS overlay may be addressed by its own name
        # without saying “панель/оверлей”: «перемести Музыка на второй экран».
        if re.match(r"^(?:перемести|перенеси|передвинь|перетащи|поставь|расположи)\b", normalized, flags=re.I):
            direct_mon = overlay_monitor(normalized)
            direct_pos = overlay_position(normalized)
            if direct_mon is not None or direct_pos is not None:
                inventory = _overlay_inventory()
                command_key = _overlay_ref_key(normalized)
                best_panel: dict[str, Any] | None = None
                best_len = 0
                for panel in inventory:
                    refs = [panel.get("name", ""), panel.get("title", ""), *(panel.get("aliases", []) or [])]
                    for ref in refs:
                        key = _overlay_ref_key(str(ref or ""))
                        if key and len(key) >= 3 and key in command_key and len(key) > best_len:
                            best_panel, best_len = panel, len(key)
                if best_panel is not None:
                    args = {"action": "move", "name": str(best_panel.get("name", "") or "")}
                    if direct_mon is not None: args["monitor"] = direct_mon
                    if direct_pos: args["position"] = direct_pos
                    return run_overlay(args)

        overlay_pronoun_move = re.match(
            r"^(?:перемести|перенеси|передвинь|перетащи|поставь|расположи)\s+(?:это|эту|этот|эти|их|его|ее|её|данное|текущее)(?:\s+(?:окно|панель|оверлей|заметки?|чек[- ]?лист))?(?:\s+(.+))?$",
            normalized, flags=re.I,
        )
        if overlay_pronoun_move:
            name = resolve_overlay_name("это окно")
            args = {"action": "move", "name": name}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            if mon is None and pos is None:
                return AgentTurn(f"Куда переместить окно «{name}»? Назови монитор и позицию, например: на третий экран слева посередине.")
            return run_overlay(args)

        overlay_show = re.match(
            r"^(покажи|открой|отобрази|скрой|спрячь|закрой)\s+(?:мне\s+)?"
            r"(чек[- ]?лист|окно|панель|оверлей|заметк\w*)(?:\s+(.+))?$",
            normalized, flags=re.I,
        )
        if overlay_show:
            verb, object_word, tail = overlay_show.group(1), overlay_show.group(2), str(overlay_show.group(3) or "").strip()
            # Strip location clauses from the persistent panel reference.
            raw_name = re.split(
                r"\s+(?:на\s+)?(?:перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|\d+\s*(?:-?й|-?м)?)\s+(?:экране?|мониторе?)\b|"
                r"\s+(?:в|с|по)\s+(?:лев\w*|прав\w*|верх\w*|ниж\w*|центр|посередине)",
                tail, maxsplit=1, flags=re.I,
            )[0].strip(' "«»,:;-')
            kind = "checklist" if "чек" in object_word.casefold() else "text" if "замет" in object_word.casefold() else ""
            purpose = _overlay_purpose_from_reference(raw_name + " " + tail, kind=kind, object_word=object_word)
            name = resolve_overlay_name(raw_name, kind=kind, purpose=purpose)
            action = "hide" if verb in {"скрой", "спрячь", "закрой"} else "show"
            args = {"action": action, "name": name}
            mon = overlay_monitor(tail); pos = overlay_position(tail)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            return run_overlay(args)

        # Natural game-session placement does not need the word "окно" or
        # "панель": "перенеси информацию по игровой сессии на третий экран".
        if (
            re.match(r"^(?:перенеси|перемести|передвинь|подвинь|поставь|расположи)\b", normalized, flags=re.I)
            and re.search(r"(?:игров\w*\s+сесси|сесси\w*\s+игр|информац\w*.*сесси)", normalized, flags=re.I)
        ):
            name = resolve_overlay_name("сессия", kind="text", purpose="game_session")
            args = {"action": "move", "name": name}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            return run_overlay(args)

        # Normalize adjective-first music references too: “перетащи музыкальный
        # оверлей...” / “перемести музыкальную панель...”.
        overlay_move_text = re.sub(
            r"\bмузыкальн\w*\s+(окно|панель|оверлей)\b", r"\1 музыка", normalized, flags=re.I
        )
        overlay_move = re.match(
            r"^(?:перемести|перенеси|передвинь|перетащи|покажи|отобрази)\s+(?:мне\s+)?"
            r"(окно|панель|оверлей|чек[- ]?лист|заметк\w*|таблиц\w*)(?:\s+(.+))?$",
            overlay_move_text, flags=re.I,
        )
        if overlay_move and (overlay_monitor(normalized) is not None or overlay_position(normalized) is not None):
            object_word, tail = overlay_move.group(1), str(overlay_move.group(2) or "").strip()
            # “окно папки/браузера/приложения ...” is a real Windows window, not
            # a JARVIS overlay.  Let the physical-window router below handle it.
            external_window_hint = (
                object_word.casefold() == "окно"
                and bool(re.match(
                    r"^(?:папк\w*|проводник\w*|браузер\w*|вкладк\w*|приложени\w*|"
                    r"chrome|хром|firefox|mozilla|edge|opera|яндекс\s+браузер)\b",
                    tail, flags=re.I,
                ))
            )
            if external_window_hint:
                overlay_move = None
            else:
                # A location clause is not a panel name.  Split before forms such as
                # “на третьем экране”, “в левом нижнем углу”, “посередине слева”.
                location_match = re.search(
                    r"(?:^|\s)(?:(?:на|в)\s+(?:(?:перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|\d+\s*(?:-?й|-?м)?)\s+(?:экране?|мониторе?)|"
                    r"(?:лев\w*|прав\w*|верх\w*|ниж\w*|центр\w*|посередине))|"
                    r"(?:слева|справа|сверху|снизу|посередине|по\s+центру)\b)",
                    tail, flags=re.I,
                )
                raw_name = tail[:location_match.start()].strip(' "«»,:;-') if location_match else tail.strip(' "«»,:;-')
                kind = "checklist" if "чек" in object_word.casefold() else "text" if "замет" in object_word.casefold() else ""
                purpose = _overlay_purpose_from_reference(raw_name + " " + tail, kind=kind, object_word=object_word)
                name = resolve_overlay_name(raw_name, kind=kind, purpose=purpose)
                args = {"action": "move", "name": name}
                mon = overlay_monitor(normalized); pos = overlay_position(normalized)
                if mon is not None: args["monitor"] = mon
                if pos: args["position"] = pos
                return run_overlay(args)

        # Relative drag/nudge without a mouse: “сдвинь окно Сессия на 80 пикселей вправо”.
        overlay_nudge = re.match(
            r"^(?:сдвинь|подвинь|передвинь|перетащи)\s+(?:окно|панель|чек[- ]?лист|заметк\w*|таблиц\w*)"
            r"(?:\s+(.+?))?\s+(?:на\s+)?(\d+)\s*(?:px|пиксел\w*)?\s+"
            r"(влево|вправо|вверх|вниз)$",
            normalized, flags=re.I,
        )
        if overlay_nudge:
            raw_name, amount_raw, direction = overlay_nudge.groups()
            spoken_ref = str(raw_name or "")
            purpose = _overlay_purpose_from_reference(spoken_ref, object_word=normalized)
            kind = "checklist" if "чек" in normalized.casefold() else "text" if "замет" in normalized.casefold() else ""
            name = resolve_overlay_name(spoken_ref, kind=kind, purpose=purpose)
            panels = _overlay_inventory()
            panel = next((p for p in panels if _overlay_ref_key(str(p.get("name", ""))) == _overlay_ref_key(name)), {})
            dx = int(panel.get("offset_x", 0) or 0); dy = int(panel.get("offset_y", 0) or 0)
            amount = min(3000, int(amount_raw))
            if direction == "влево": dx -= amount
            elif direction == "вправо": dx += amount
            elif direction == "вверх": dy -= amount
            else: dy += amount
            return run_overlay({"action": "move", "name": name, "offset_x": dx, "offset_y": dy})

        overlay_pronoun_style = re.match(
            r"^(?:измени|поменяй|поставь|сделай)\s+(размер\s+шрифта|прозрачност\w*|ширин\w*|высот\w*)\s+(?:в\s+|для\s+)?(?:этом|этом\s+же|текущем|данном)\s+(?:окне|панели)(?:\s+(?:на|в))?\s+(\d+)\s*(?:%|px|пиксел\w*)?$",
            normalized, flags=re.I,
        )
        if overlay_pronoun_style is None:
            # Natural genitive phrasing: “сделай размер шрифта этого окна 22”,
            # “поставь прозрачность этой панели на 60 процентов”.
            overlay_pronoun_style = re.match(
                r"^(?:измени|поменяй|поставь|сделай)\s+(размер\s+шрифта|прозрачност\w*|ширин\w*|высот\w*)\s+"
                r"(?:этого|текущего|данного)\s+(?:окна|чек[- ]?листа)|"
                r"^(?:измени|поменяй|поставь|сделай)\s+(размер\s+шрифта|прозрачност\w*|ширин\w*|высот\w*)\s+"
                r"(?:этой|текущей|данной)\s+панели",
                normalized, flags=re.I,
            )
            if overlay_pronoun_style:
                # The compact alternative above only identifies the property;
                # parse the numeric tail separately to keep the grammar lenient.
                prop = next((g for g in overlay_pronoun_style.groups() if g), "")
                value_match = re.search(r"(\d+)\s*(?:%|px|пиксел\w*|процент\w*)?\s*$", normalized, flags=re.I)
                if value_match:
                    name = resolve_overlay_name("это окно")
                    key = "font_size" if "шрифт" in prop else "opacity" if "прозрач" in prop else "height" if "высот" in prop else "width"
                    return run_overlay({"action": "style", "name": name, key: int(value_match.group(1))})
                overlay_pronoun_style = None
        if overlay_pronoun_style:
            prop, value_raw = overlay_pronoun_style.groups()
            name = resolve_overlay_name("это окно")
            key = "font_size" if "шрифт" in prop else "opacity" if "прозрач" in prop else "height" if "высот" in prop else "width"
            return run_overlay({"action": "style", "name": name, key: int(value_raw)})

        overlay_pronoun_color = re.match(
            r"^(?:измени|поменяй|поставь|сделай)\s+(?:цвет\s+)?(текста|шрифта|фон(?:а)?|рамк(?:и)?)\s+(?:в\s+|для\s+)?(?:этом|текущем|данном)\s+(?:окне|панели)\s+(?:на\s+)?([a-zа-яё#0-9]+)$",
            normalized, flags=re.I,
        )
        if overlay_pronoun_color is None:
            overlay_pronoun_color = re.match(
                r"^(?:измени|поменяй|поставь|сделай)\s+(?:цвет\s+)?(текста|шрифта|фон(?:а)?|рамк(?:и)?)\s+"
                r"(?:этого\s+окна|этой\s+панели|текущего\s+окна|текущей\s+панели)\s+(?:на\s+)?([a-zа-яё#0-9]+)$",
                normalized, flags=re.I,
            )
        if overlay_pronoun_color:
            part, color = overlay_pronoun_color.groups()
            name = resolve_overlay_name("это окно")
            key = "text_color" if part.startswith(("текст", "шрифт")) else "background_color" if part.startswith("фон") else "border_color"
            return run_overlay({"action": "style", "name": name, key: color})

        overlay_window_color = re.match(
            r"^(?:измени|поменяй|поставь|сделай)\s+(?:цвет\s+)?(?:этого\s+окна|этой\s+панели)\s+(?:на\s+)?([a-zа-яё#0-9]+)$",
            normalized, flags=re.I,
        )
        if overlay_window_color:
            name = resolve_overlay_name("это окно")
            return run_overlay({"action": "style", "name": name, "background_color": overlay_window_color.group(1)})

        overlay_color = re.match(
            r"^(?:измени|поменяй|поставь|сделай)\s+(?:цвет\s+)?(текста|шрифта|фон(?:а)?|рамк(?:и)?)\s+"
            r"(окна|панели|чек[- ]?листа|заметок|таблиц\w*)(?:\s+(.+?))?\s+(?:на\s+)?([a-zа-яё#0-9]+)$",
            normalized, flags=re.I,
        )
        if overlay_color:
            part, object_word, raw_name, color = overlay_color.groups()
            kind = "checklist" if "чек" in object_word.casefold() else "text" if "замет" in object_word.casefold() else ""
            purpose = _overlay_purpose_from_reference(str(raw_name or ""), kind=kind, object_word=object_word)
            name = resolve_overlay_name(str(raw_name or ""), kind=kind, purpose=purpose)
            key = "text_color" if part.startswith(("текст", "шрифт")) else "background_color" if part.startswith("фон") else "border_color"
            return run_overlay({"action": "style", "name": name, key: color})

        # Useful desktop primitive for future game tools as well: a caller can
        # compose session/build text and ask JARVIS to render it at a chosen slot.
        overlay_text = re.match(
            r"^(?:покажи|отобрази)\s+(?:в\s+(?:окне|панели)\s+)?(?:текст|сообщение)\s+[\"«]?(.+?)[\"»]?"
            r"(?:\s+на\s+.*(?:экране?|мониторе?).*)?$",
            normalized, flags=re.I,
        )
        if overlay_text:
            body = overlay_text.group(1).strip(' "«»')
            body = re.split(r"\s+на\s+(?:перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|\d+).*?(?:экран|монитор)", body, maxsplit=1, flags=re.I)[0].strip()
            name = resolve_overlay_name("")
            args = {"action": "set_text", "name": name, "title": name, "text": body}
            mon = overlay_monitor(normalized); pos = overlay_position(normalized)
            if mon is not None: args["monitor"] = mon
            if pos: args["position"] = pos
            return run_overlay(args)

        # Natural object-first font phrasing: “поменяй шрифт в чек-листе по игре на Arial”.
        # Here “по игре” is a semantic reference, never a literal window title.
        overlay_font_in_object = re.match(
            r"^(?:измени|поменяй|поставь|сделай)\s+(?:стиль\s+)?шрифт(?:а)?\s+"
            r"(?:в|для)\s+(окне|панели|чек[- ]?листе|заметках|таблице)"
            r"(?:\s+(.+?))?\s+(?:на|в)\s+([a-zа-яё0-9 _-]{2,50})$",
            normalized, flags=re.I,
        )
        if overlay_font_in_object:
            object_word, raw_name, family = overlay_font_in_object.groups()
            spoken_ref = str(raw_name or "").strip(' "«»')
            kind = "checklist" if "чек" in object_word.casefold() else "text" if "замет" in object_word.casefold() else ""
            purpose = _overlay_purpose_from_reference(spoken_ref, kind=kind, object_word=object_word + " " + normalized)
            name = resolve_overlay_name(spoken_ref, kind=kind, purpose=purpose)
            return run_overlay({"action": "style", "name": name, "font_family": family.strip()})

        overlay_font_pronoun = re.match(
            r"^(?:измени|поменяй|поставь|сделай)\s+(?:стиль\s+)?шрифт(?:а)?\s+(?:в\s+|для\s+)?(?:этом|этом\s+же|этом\s+моем|моём|этом\s+текущем)?\s*(?:окне|панели|чек[- ]?листе|заметках)(?:\s+(?:на|в)\s+(.+))?$",
            normalized, flags=re.I,
        )
        if overlay_font_pronoun is None:
            overlay_font_pronoun = re.match(
                r"^(?:измени|поменяй|поставь|сделай)\s+(?:стиль\s+)?шрифт(?:а)?\s+"
                r"(?:этого\s+окна|этой\s+панели|этого\s+чек[- ]?листа|этих\s+заметок)"
                r"(?:\s+(?:на|в)\s+(.+))?$", normalized, flags=re.I,
            )
        if overlay_font_pronoun:
            family = str(overlay_font_pronoun.group(1) or "").strip(' "«»')
            name = resolve_overlay_name("это окно")
            if not family:
                self._set_pending_overlay_followup("font_family", name)
                return AgentTurn(f"Какой шрифт поставить в окне «{name}»? Например: Segoe UI, Arial или Consolas.")
            return run_overlay({"action": "style", "name": name, "font_family": family})

        overlay_font_family = re.match(
            r"^(?:поставь|сделай|измени)\s+шрифт\s+([a-zа-яё0-9 _-]{2,50}?)\s+"
            r"(?:для\s+|в\s+)?(?:окне|окна|панели|панели|чек[- ]?листе|чек[- ]?листа|заметках|заметок)(?:\s+(.+))?$",
            normalized, flags=re.I,
        )
        if overlay_font_family:
            family, raw_name = overlay_font_family.groups()
            spoken_ref = str(raw_name or "")
            kind = "checklist" if "чек" in normalized.casefold() else "text" if "замет" in normalized.casefold() else ""
            purpose = _overlay_purpose_from_reference(spoken_ref, kind=kind, object_word=normalized)
            name = resolve_overlay_name(spoken_ref, kind=kind, purpose=purpose)
            return run_overlay({"action": "style", "name": name, "font_family": family.strip()})

        overlay_font_family_alt = re.match(
            r"^(?:измени|поставь|сделай)\s+(?:стиль\s+)?шрифт(?:а)?\s+"
            r"(?:окна|панели|чек[- ]?листа|заметок|таблиц\w*)(?:\s+(.+?))?\s+(?:на|в)\s+([a-zа-яё0-9 _-]{2,50})$",
            normalized, flags=re.I,
        )
        if overlay_font_family_alt:
            raw_name, family = overlay_font_family_alt.groups()
            # “на Arial” is a style value, not the panel identity. If no explicit
            # name was spoken, the last/visible panel of the requested kind wins.
            spoken_ref = str(raw_name or "")
            kind = "checklist" if "чек" in normalized.casefold() else "text" if "замет" in normalized.casefold() else ""
            purpose = _overlay_purpose_from_reference(spoken_ref, kind=kind, object_word=normalized)
            name = resolve_overlay_name(spoken_ref, kind=kind, purpose=purpose)
            return run_overlay({"action": "style", "name": name, "font_family": family.strip()})

        overlay_click_mode = re.match(
            r"^(?:сделай\s+(?:окно|панель|чек[- ]?лист|заметки?)\s*(.*?)\s+(перетаскиваем\w*|кликабельн\w*)|"
            r"(разреши|запрети|не\s+пропускай|пропускай)\s+(?:мыш\w*\s+|клики\s+)?(?:через\s+|по\s+)?(?:окно|панель|чек[- ]?лист|заметки?)\s*(.*))$",
            normalized, flags=re.I,
        )
        if overlay_click_mode:
            if overlay_click_mode.group(2):
                spoken_ref = str(overlay_click_mode.group(1) or "").strip(' "«»')
                click_through = False
            else:
                verb = str(overlay_click_mode.group(3) or "").casefold()
                spoken_ref = str(overlay_click_mode.group(4) or "").strip(' "«»')
                click_through = verb == "пропускай"
            kind = "checklist" if "чек" in normalized.casefold() else "text" if "замет" in normalized.casefold() else ""
            purpose = _overlay_purpose_from_reference(spoken_ref, kind=kind, object_word=normalized)
            name = resolve_overlay_name(spoken_ref, kind=kind, purpose=purpose)
            return run_overlay({"action": "style", "name": name, "click_through": click_through})

        overlay_style = re.match(
            r"^(?:измени|поменяй|поставь|сделай)\s+(размер\s+шрифта|прозрачност\w*|ширин\w*|высот\w*)\s+"
            r"(?:окна|панели|чек[- ]?листа|заметок|таблиц\w*)(?:\s+(.+?))?\s+(?:на\s+)?(\d+)\s*(?:%|px|пиксел\w*)?$",
            normalized, flags=re.I,
        )
        if overlay_style:
            prop, raw_name, value_raw = overlay_style.groups()
            spoken_ref = str(raw_name or "")
            kind = "checklist" if "чек" in normalized.casefold() else "text" if "замет" in normalized.casefold() else ""
            purpose = _overlay_purpose_from_reference(spoken_ref, kind=kind, object_word=normalized)
            name = resolve_overlay_name(spoken_ref, kind=kind, purpose=purpose)
            value = int(value_raw)
            key = "font_size" if "шрифт" in prop else "opacity" if "прозрач" in prop else "height" if "высот" in prop else "width"
            return run_overlay({"action": "style", "name": name, key: value})

        overlay_window_size = re.match(
            r"^(?:измени|поменяй|сделай|поставь)\s+(?:размер\s+)?(?:этого\s+|текущего\s+)?(?:окна|панели|чек[- ]?листа|заметок)?"
            r"(?:\s+(.+?))?\s+(?:на\s+|размером\s+)?(\d+)\s*(?:x|х|на)\s*(\d+)\s*(?:px|пиксел\w*)?$",
            normalized, flags=re.I,
        )
        if overlay_window_size:
            raw_name, width_raw, height_raw = overlay_window_size.groups()
            name = resolve_overlay_name(str(raw_name or "это окно"))
            return run_overlay({"action": "style", "name": name, "width": int(width_raw), "height": int(height_raw)})

        overlay_relative_font = re.match(
            r"^(увеличь|уменьши)\s+(?:размер\s+)?шрифт(?:а)?\s+(?:окна|панели|чек[- ]?листа|заметок)?"
            r"(?:\s+(?!на\s+\d+\b)(.+?))?(?:\s+на\s+(\d+))?$",
            normalized, flags=re.I,
        )
        if overlay_relative_font:
            verb, raw_name, delta_raw = overlay_relative_font.groups()
            spoken_ref = str(raw_name or "")
            kind = "checklist" if "чек" in normalized.casefold() else "text" if "замет" in normalized.casefold() else ""
            purpose = _overlay_purpose_from_reference(spoken_ref, kind=kind, object_word=normalized)
            name = resolve_overlay_name(spoken_ref, kind=kind, purpose=purpose)
            panels = _overlay_inventory()
            panel = next((p for p in panels if normalize_target(str(p.get("name", ""))) == normalize_target(name)), {})
            style = panel.get("style", {}) if isinstance(panel, dict) and isinstance(panel.get("style"), dict) else {}
            current = int(style.get("font_size", 18) or 18)
            delta = max(1, min(24, int(delta_raw or 2)))
            target = current + delta if verb == "увеличь" else current - delta
            return run_overlay({"action": "style", "name": name, "font_size": max(8, min(96, target))})

        overlay_clear_notes = re.match(r"^очисти\s+(?:игровые\s+)?заметки?(?:\s+(.+))?$", normalized, flags=re.I)
        if overlay_clear_notes:
            spoken_ref = str(overlay_clear_notes.group(1) or "")
            purpose = _overlay_purpose_from_reference(spoken_ref, kind="text", object_word="заметки")
            name = resolve_overlay_name(spoken_ref, kind="text", purpose=purpose)
            return run_overlay({"action": "clear_text", "name": name})

        # Compound folder -> create -> write workflow. Keep it deterministic so
        # a long voice command never falls through to the LLM and loses "там/него".
        compound_create = re.match(
            r"^(?:открой|открыть)\s+(?:папк\w*\s+)?(.+?)[,;]?\s+"
            r"(?:и\s+)?создай\s+там\s+(?:(txt|текстов\w*|markdown|md|json|csv)\s+)?"
            r"(?:файл\w*\s+)?(.+?)(?:\s+(?:и\s+)?(?:запиши|напиши|впиши)\s+(?:в\s+(?:него|файл)\s+)?(.+))?$",
            normalized, flags=re.I,
        )
        if compound_create:
            folder_query = compound_create.group(1).strip(' "«».,;')
            file_name = created_filename(compound_create.group(3), compound_create.group(2) or "")
            content = str(compound_create.group(4) or "").strip()
            folder_res = self.tools.execute("desktop", {"action": "resolve", "query": folder_query, "hint": "folder"})
            if not folder_res.success or not isinstance(folder_res.data, dict):
                return AgentTurn(folder_res.text, tool_details=[f"desktop: {folder_res.text}"])
            folder_path = str(folder_res.data.get("value", "") or "")
            if not folder_path:
                return AgentTurn(f"Не удалось определить папку «{folder_query}».")
            open_args = {"action": "open", "source": folder_path}
            opened = self.tools.execute("filesystem", open_args)
            self._remember_execution_context("filesystem", open_args, opened)
            create_args = {"action": "create_text_file", "folder": folder_path, "name": file_name, "content": content}
            created = self.tools.execute("filesystem", create_args)
            self._remember_execution_context("filesystem", create_args, created)
            return AgentTurn(f"{opened.text}. {created.text}", tool_details=[f"filesystem: {opened.text}", f"filesystem: {created.text}"])

        # Last guard for JARVIS-owned panels. If a phrase clearly addresses
        # notes/checklists but none of the deterministic grammars above matched,
        # never hand it to filesystem/desktop/LLM where “заметки” can become a
        # filename or a foreign application panel. Ask for the missing detail.
        if (
            re.search(r"\b(?:заметк\w*|чек[- ]?лист\w*)\b", normalized, flags=re.I)
            and not re.search(r"\b(?:файл|документ)\w*\b", normalized, flags=re.I)
        ):
            from jarvis.runtime.dialogue_messages import message
            from jarvis.language import language_code
            return AgentTurn(message("panel_clarification", language_code(self.config)), outcome="clarification")

        # ---------- RC21 deterministic file-context workflow ----------
        # Resolve folder + file requests before the generic desktop resolver/LLM.
        # This covers natural forms such as:
        #   "в папке Important тебе нужно открыть file test.txt"
        #   "открой файл test txt в папке Important"
        # and a frequent Whisper rendering "pubkey Important" for "в папке Important".
        folder_then_file = re.match(
            r"^(?:в|из)\s+папк\w*\s+(.+?)\s+(?:(?:тебе|мне)\s+)?(?:нужно|надо|следует)?\s*"
            r"(?:открой|открыть|запусти|запустить|найди|найти)\s+(?:файл\w*\s+)?(.+)$",
            normalized, flags=re.I,
        )
        file_then_folder = re.match(
            r"^(?:открой|запусти|найди)\s+(?:файл\w*\s+)?(.+?)\s+(?:в|из)\s+папк\w*\s+(.+)$",
            normalized, flags=re.I,
        )
        pubkey_form = re.match(
            r"^(?:открой|запусти|найди)\s+(?:файл\w*\s+)?(.+?)\s+pubkey\s+(.+)$",
            normalized, flags=re.I,
        )
        if folder_then_file or file_then_folder or pubkey_form:
            if folder_then_file:
                folder_query, file_query = folder_then_file.group(1), folder_then_file.group(2)
            else:
                file_query, folder_query = (file_then_folder or pubkey_form).group(1), (file_then_folder or pubkey_form).group(2)
            # Stop accidental capture of filler words in either literal name.
            folder_query = re.sub(r"\s+(?:тебе|мне)\s+(?:нужно|надо).*$", "", folder_query, flags=re.I).strip(' "«».,;')
            file_query = self._normalize_spoken_filename(file_query)
            folder_res = self.tools.execute("desktop", {"action": "resolve", "query": folder_query, "hint": "folder"})
            if not folder_res.success or not isinstance(folder_res.data, dict):
                return AgentTurn(folder_res.text, tool_details=[f"desktop: {folder_res.text}"])
            folder_path = str(folder_res.data.get("value", "") or "")
            if not folder_path:
                return AgentTurn(f"Не удалось определить папку «{folder_query}».")
            folder_args = {"action": "open", "source": folder_path}
            opened_folder = self.tools.execute("filesystem", folder_args)
            self._remember_execution_context("filesystem", folder_args, opened_folder)
            candidate = self._resolve_context_file(file_query)
            if not candidate:
                return AgentTurn(f"Папка открыта: {folder_path}. Файл «{file_query}» в ней не найден.", tool_details=[f"filesystem: {opened_folder.text}"])
            file_args = {"action": "open", "source": candidate}
            opened_file = self.tools.execute("filesystem", file_args)
            self._remember_execution_context("filesystem", file_args, opened_file)
            return AgentTurn(f"Папка: {folder_path}. Файл открыт: {candidate}", tool_details=[f"filesystem: {opened_folder.text}", f"filesystem: {opened_file.text}"])

        # ---------- RC23 deterministic named-folder write workflow ----------
        # Examples:
        #   "в папке Important file test.txt в него нужно написать 123"
        #   "в папке Important в test.txt запиши 123"
        folder_file_write = re.match(
            r"^(?:в|из)\s+папк\w*\s+(.+?)\s+(?:file|файл\w*)\s+(.+?)\s+"
            r"(?:в\s+(?:него|этот\s+файл)\s+)?(?:(?:мне|тебе)\s+)?(?:нужно|надо|следует)?\s*"
            r"(?:написать|напиши|записать|запиши|добавить|добавь|вписать|впиши)\s+(.+)$",
            normalized, flags=re.I,
        )
        if not folder_file_write:
            folder_file_write = re.match(
                r"^(?:в|из)\s+папк\w*\s+(.+?)\s+в\s+(.+?)\s+"
                r"(?:написать|напиши|записать|запиши|добавить|добавь|вписать|впиши)\s+(.+)$",
                normalized, flags=re.I,
            )

        if folder_file_write:
            folder_query = folder_file_write.group(1).strip(' "«».,;')
            file_query = self._normalize_spoken_filename(folder_file_write.group(2))
            content = folder_file_write.group(3).strip()
            folder_res = self.tools.execute("desktop", {"action": "resolve", "query": folder_query, "hint": "folder"})
            if not folder_res.success or not isinstance(folder_res.data, dict):
                return AgentTurn(folder_res.text, tool_details=[f"desktop: {folder_res.text}"])
            folder_path = str(folder_res.data.get("value", "") or "")
            from pathlib import Path as _Path
            direct = _Path(folder_path) / file_query
            if not direct.is_file():
                find_res = self.tools.execute("filesystem", {"action": "find", "source": folder_path, "query": file_query, "recursive": False, "limit": 10})
                if find_res.success and isinstance(find_res.data, list) and find_res.data:
                    direct = _Path(find_res.data[0])
            if not direct.is_file():
                return AgentTurn(f"В папке {folder_path} не нашёл файл {file_query}.")
            self._last_folder_path = folder_path
            self._last_file_path = str(direct)
            self._last_opened_file_path = str(direct)
            write_args = {"action": "write_text", "source": str(direct), "content": content, "append": True}
            execution = self.tools.execute("filesystem", write_args)
            self._remember_execution_context("filesystem", write_args, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # File-first form used naturally in mixed Russian/English speech:
        # «file test.txt в папке Important напиши 123». Resolve the named folder
        # first and never reinterpret the whole sentence as a filename.
        file_folder_write = re.match(
            r"^(?:file|файл\w*)\s+(.+?)\s+(?:в|из)\s+папк\w*\s+(.+?)\s+"
            r"(?:написать|напиши|записать|запиши|добавить|добавь|вписать|впиши)\s+(.+)$",
            normalized, flags=re.I,
        )
        if file_folder_write:
            from pathlib import Path as _Path
            file_query = self._normalize_spoken_filename(file_folder_write.group(1))
            folder_query = file_folder_write.group(2).strip(' "«».,;')
            content = file_folder_write.group(3).strip()
            folder_res = self.tools.execute("desktop", {"action": "resolve", "query": folder_query, "hint": "folder"})
            if not folder_res.success or not isinstance(folder_res.data, dict):
                return AgentTurn(folder_res.text, tool_details=[f"desktop: {folder_res.text}"])
            folder_path = str(folder_res.data.get("value", "") or "")
            if not folder_path:
                return AgentTurn(f"Не удалось определить папку «{folder_query}».")
            direct = (_Path(folder_path) / file_query).resolve(strict=False)
            if not direct.is_file():
                find_res = self.tools.execute("filesystem", {"action": "find", "source": folder_path, "query": file_query, "recursive": False, "limit": 10})
                if find_res.success and isinstance(find_res.data, list) and find_res.data:
                    direct = _Path(str(find_res.data[0]))
            if not direct.is_file():
                return AgentTurn(f"В папке {folder_path} не нашёл файл {file_query}.")
            self._last_folder_path = folder_path
            self._last_file_path = str(direct)
            self._last_opened_file_path = str(direct)
            args = {"action": "write_text", "source": str(direct), "content": content, "append": True}
            execution = self.tools.execute("filesystem", args)
            self._remember_execution_context("filesystem", args, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # If the user points at an already-open file on screen, prefer the remembered
        # open-file context and the literal basename over any global recursive search.
        screen_file_write = re.match(
            r"^(?:смотри[, ]+)?(?:на\s+экране\s+(?:у\s+меня\s+)?)?"
            r"(?:открыт\w*\s+)?(?:файл\w*\s+)?(?:под\s+названи\w*\s+)?"
            r"(.+?)(?:\s+и)?\s+(?:(?:мне|тебе)\s+)?(?:нужно|надо)?\s*(?:чтобы\s+ты\s+)?"
            r"(?:в\s+(?:него|этот\s+файл)\s+)?"
            r"(?:вписал|впиши|написал|напиши|записал|запиши|добавил|добавь)\s+(.+)$",
            normalized, flags=re.I,
        )
        if screen_file_write:
            from pathlib import Path as _Path
            file_query = self._normalize_spoken_filename(screen_file_write.group(1))
            content = screen_file_write.group(2).strip()
            target_path = self._resolve_context_file(file_query)
            if not target_path:
                # If Explorer is foreground, synchronize its directory and try only
                # that exact folder. No project-wide recursive search.
                explorer = self._foreground_explorer_path()
                if explorer:
                    self._last_folder_path = explorer
                    candidate = _Path(explorer) / file_query
                    if candidate.is_file():
                        target_path = str(candidate)
            if not target_path:
                return AgentTurn(f"Не смог однозначно определить открытый файл {file_query}. Открой его или папку с ним и повтори команду.")
            write_args = {"action": "write_text", "source": target_path, "content": content, "append": True}
            execution = self.tools.execute("filesystem", write_args)
            self._remember_execution_context("filesystem", write_args, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # Explicit references to the file created/opened earlier must never be sent
        # to desktop fuzzy search as a literal phrase.
        contextual_open = re.match(
            r"^(?:открой|запусти)\s+(?:файл\w*\s+)?(.+)$", normalized, flags=re.I
        )
        if contextual_open:
            reference_text = contextual_open.group(1).strip()
            referenced = self._context_reference_file(reference_text)
            if referenced:
                args = {"action": "open", "source": referenced}
                execution = self.tools.execute("filesystem", args)
                self._remember_execution_context("filesystem", args, execution)
                return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])
            # A spoken filename without punctuation ("test txt") is still a file
            # when there is an active folder/file workflow.
            cleaned_file = self._normalize_spoken_filename(reference_text)
            if getattr(self, "_last_folder_path", "") and re.search(r"\.(?:txt|md|json|csv|pdf|docx)$", cleaned_file, flags=re.I):
                candidate = self._resolve_context_file(cleaned_file)
                if candidate:
                    args = {"action": "open", "source": candidate}
                    execution = self.tools.execute("filesystem", args)
                    self._remember_execution_context("filesystem", args, execution)
                    return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # Direct Windows radio controls. "выключи Bluetooth" must perform the
        # action, not merely navigate to the Settings page.
        bt = re.match(
            r"^(?:(?:джарвис|нео|нио|neo)[, ]+)?(включи|выключи|отключи)\s+(?:блютуз|bluetooth)"
            r"(?:\s+(?:в\s+системе|в\s+windows|на\s+(?:компьютере|пк)))?$",
            normalized,
        )
        if bt:
            enabled = bt.group(1) == "включи"
            execution = self.tools.execute("system", {"action": "set_bluetooth", "enabled": enabled})
            self._remember_execution_context("system", {"action": "set_bluetooth", "enabled": enabled}, execution)
            return AgentTurn(execution.text, tool_details=[f"system: {execution.text}"])
        wifi = re.match(
            r"^(?:(?:джарвис|нео|нио|neo)[, ]+)?(включи|выключи|отключи)\s+(?:вай ?фай|wi ?fi|wifi)"
            r"(?:\s+(?:в\s+системе|в\s+windows|на\s+(?:компьютере|пк)))?$",
            normalized,
        )
        if wifi:
            enabled = wifi.group(1) == "включи"
            execution = self.tools.execute("system", {"action": "set_wifi", "enabled": enabled})
            self._remember_execution_context("system", {"action": "set_wifi", "enabled": enabled}, execution)
            return AgentTurn(execution.text, tool_details=[f"system: {execution.text}"])
        bright = re.match(r"^(?:сделай|установи|поставь)\s+яркост\w*\s+(\d{1,3})(?:\s*%|\s*процент\w*)?$", normalized)
        if bright:
            value = max(0, min(100, int(bright.group(1))))
            execution = self.tools.execute("system", {"action": "set_brightness", "value": value})
            return AgentTurn(execution.text, tool_details=[f"system: {execution.text}"])

        # v3.9: move a real top-level window between physical monitors.  Resolve
        # the *kind* (browser / Explorer folder / active window) separately from
        # its title, and keep an optional source monitor as a hard filter.  This
        # makes “перенеси браузер со второго экрана на третий” deterministic even
        # when several Chrome/Explorer windows are open on different displays.
        move_prefix = re.match(r"^(?:перенеси|перемести|отправь|перетащи|передвинь|подвинь)\s+(.+)$", normalized, flags=re.I)
        if move_prefix:
            move_tail = str(move_prefix.group(1) or "").strip()

            def _spoken_monitor_number(fragment: str) -> int | None:
                value = str(fragment or "").casefold().replace("ё", "е")
                digit = re.search(r"(?<!\d)(\d{1,2})(?:\s*(?:-?й|-?м|-?го))?", value)
                if digit:
                    return int(digit.group(1))
                stems = {
                    "одн": 1, "перв": 1, "втор": 2, "трет": 3, "четвер": 4,
                    "пят": 5, "шест": 6, "седьм": 7, "восьм": 8,
                }
                for stem, number in stems.items():
                    if re.search(rf"\b{stem}\w*\b", value):
                        return number
                return None

            destination_match = re.search(
                r"\bна\s+((?:\d{1,2}(?:\s*(?:-?й|-?м|-?й|-?го))?|"
                r"(?:одн|перв|втор|трет|четвер|пят|шест|седьм|восьм)\w*))\s+"
                r"(?:экран\w*|монитор\w*)\s*$",
                move_tail, flags=re.I,
            )
            if destination_match:
                monitor = _spoken_monitor_number(destination_match.group(1))
                before_destination = move_tail[:destination_match.start()].strip(" ,.-")
                source_monitor = 0
                source_match = re.search(
                    r"\b(?:с|со)\s+((?:\d{1,2}(?:\s*(?:-?го|-?й|-?м))?|"
                    r"(?:одн|перв|втор|трет|четвер|пят|шест|седьм|восьм)\w*))"
                    r"(?:\s+(?:экрана?\w*|монитора?\w*))?\s*$",
                    before_destination, flags=re.I,
                )
                if source_match:
                    source_monitor = int(_spoken_monitor_number(source_match.group(1)) or 0)
                    subject = before_destination[:source_match.start()].strip(" ,.-")
                else:
                    subject = before_destination

                subject = re.sub(r"^(?:это|этот|эту|текущее|текущий|текущую)\s+", "", subject, flags=re.I).strip()
                subject = re.sub(r"\b(?:пожалуйста|там)\b", " ", subject, flags=re.I)
                subject = re.sub(r"\s+", " ", subject).strip(" ,.-")
                kind = ""
                title = subject

                browser_subject = re.match(
                    r"^(?:окно\s+)?(?:браузер\w*|вкладк\w*)(?:\s+(.*))?$",
                    subject, flags=re.I,
                )
                folder_subject = re.match(
                    r"^(?:окно\s+)?(?:папк\w*|проводник\w*|file\s+explorer)(?:\s+(.*))?$",
                    subject, flags=re.I,
                )
                if browser_subject:
                    kind = "browser"
                    tail = str(browser_subject.group(1) or "").strip()
                    title = tail or "browser"
                elif folder_subject:
                    kind = "folder"
                    tail = str(folder_subject.group(1) or "").strip()
                    title = tail or "folder"
                elif re.fullmatch(r"(?:окно|его|это|текущее)", subject, flags=re.I):
                    title = "active"
                else:
                    title = re.sub(r"^окно\s+", "", title, flags=re.I).strip() or "active"

                if monitor:
                    args = {"action": "move_to_monitor", "title": title, "monitor": int(monitor)}
                    if source_monitor:
                        args["source_monitor"] = source_monitor
                    if kind:
                        args["kind"] = kind
                    execution = self.tools.execute("window", args)
                    return AgentTurn(execution.text, tool_details=[f"window: {execution.text}"])

        move_current_tab = re.match(
            r"^(?:открой|перенеси|перемести|отправь)\s+(?:эту\s+|текущую\s+)?вкладку\s+на\s+(\d+)\s*(?:-?й|-?м)?\s*(?:экран\w*|монитор\w*)$",
            normalized,
        )
        if move_current_tab:
            monitor = int(move_current_tab.group(1))
            execution = self.tools.execute("window", {"action": "move_to_monitor", "title": "browser", "kind": "browser", "monitor": monitor})
            return AgentTurn(execution.text, tool_details=[f"window: {execution.text}"])

        # Search, then move the browser as a second deterministic step.
        search_move = re.match(
            r"^найди\s+в\s+браузер\w*\s+(.+?)\s+(?:и|затем|потом)\s+(?:открой|перенеси|перемести)\s+(?:вкладку|браузер|окно)?\s*на\s+(\d+)\s*(?:-?й|-?м)?\s*(?:экран\w*|монитор\w*)$",
            normalized,
        )
        if search_move:
            query = search_move.group(1).strip()
            monitor = int(search_move.group(2))
            first = self.tools.execute("browser", {"action": "search", "value": query})
            import time as _time
            _time.sleep(0.35)
            second = self.tools.execute("window", {"action": "move_to_monitor", "title": "browser", "monitor": monitor})
            return AgentTurn(f"{first.text} {second.text}", tool_details=[f"browser: {first.text}", f"window: {second.text}"])

        # Voice workflow: “на экране 1 открой браузер и найди X, перейди по первой ссылке”.
        # Browser identity is intentionally pinned to Settings; a spoken browser
        # name is treated as conversational noise so automation never jumps apps.
        search_first = re.match(
            r"^(?:на\s+(\d+)\s*(?:-?м|-?й)?\s*(?:экране|мониторе)[, ]+)?"
            r"(?:(?:открой|запусти)\s+(?:браузер\w*|мозил\w*|firefox|chrome|хром|opera|опера)\s+(?:и\s+)"
            r"найди\s+(?:там\s+)?|найди\s+в\s+браузер\w*\s+)(.+?)\s+(?:и\s+|затем\s+|потом\s+)?"
            r"(?:перейди|открой|зайди)\s+(?:по\s+)?перв\w*\s+ссылк\w*$",
            normalized,
        )
        if search_first:
            monitor = int(search_first.group(1) or 0)
            query = search_first.group(2).strip()
            if monitor:
                # Best effort: move the configured browser first; if it is not yet
                # open, the subsequent browser operation will launch it.
                self.tools.execute("window", {"action": "move_to_monitor", "title": "browser", "monitor": monitor})
            execution = self.tools.execute("browser", {"action": "search_first", "value": query})
            self._remember_execution_context("browser", {"action": "search_first", "value": query}, execution)
            return AgentTurn(execution.text, tool_details=[f"browser: {execution.text}"])

        # HOTFIX82: screen translation is a separate visual intent. Resolve it
        # before generic screen analysis and before plain-text translation so
        # «переведи текст с/на экрана» always translates captured pixels, never
        # the literal words of the request.
        screen_translation_requested = bool(
            re.search(r"\b(?:переведи|перевести|перевод|translate)\w*\b", normalized, re.I)
            and re.search(r"\b(?:экран|монитор|скриншот)\w*\b", normalized, re.I)
        ) or bool(
            re.search(r"\b(?:экран|монитор|скриншот)\w*\b.{0,55}\b(?:переведи|перевести|перевод)\w*\b", normalized, re.I)
        )
        if screen_translation_requested:
            language = "английский" if re.search(r"англ|english", normalized, re.I) else (
                "русский" if re.search(r"русск|russian", normalized, re.I) else "русский"
            )
            monitor = requested_monitor(normalized)
            args = {"action": "translate", "target_language": language, "monitor": monitor}
            execution = self.tools.execute("screen", args)
            self._remember_execution_context("screen", args, execution)
            return AgentTurn(execution.text, tool_details=[f"screen: {execution.text}"])

        # Vision is always direct: capture first, ask the vision model second.
        screen_needles = (
            "что ты видишь", "что видишь", "что на экране", "что сейчас на экране",
            "посмотри на экран", "посмотри экран", "что тут", "что здесь",
            "как решить эту головоломку", "реши головоломку", "в чем шутка",
            "что мне сделать следующим", "что делать дальше", "объясни что происходит",
            "прочитай что на экране", "разбери экран",
        )
        screen_requested = any(item in normalized for item in screen_needles) or bool(
            re.search(r"(?:что|покажи|прочитай|объясни|опиши).{0,35}(?:экран|монитор)|(?:экран|монитор).{0,35}(?:что|переведи|прочитай|объясни)", normalized)
        )
        if screen_requested:
            monitor = requested_monitor(normalized)
            execution = self.tools.execute(
                "screen",
                {"action": "analyze", "instruction": text, "monitor": monitor},
            )
            self._remember_execution_context("screen", {"action": "analyze", "monitor": monitor}, execution)
            return AgentTurn(execution.text, tool_details=[f"screen: {execution.text}"])

        if ("переведи" in normalized or "как это переводится" in normalized) and any(
            item in normalized for item in ("экран", "это", "текст", "надпись")
        ):
            if self._last_visual_context and "экран" not in normalized and re.match(r"^переведи\s+(?:мне\s+)?(?:это|этот\s+текст)", normalized):
                # Preserve conversational reference to the previously inspected
                # screen instead of silently recapturing a different current view.
                return None
            language = "английский" if re.search(r"англ|english", normalized) else (
                "русский" if re.search(r"русск|russian", normalized) else "русский"
            )
            monitor = requested_monitor(normalized)
            # When the request contains more than translation (e.g. "что на
            # первом экране и переведи"), analyze with the full instruction so
            # the vision model completes every clause.
            if any(item in normalized for item in ("что на", "что вид", "объясни", "опиши")):
                args = {"action": "analyze", "instruction": text, "monitor": monitor}
            else:
                args = {"action": "translate", "target_language": language, "monitor": monitor}
            execution = self.tools.execute("screen", args)
            self._remember_execution_context("screen", args, execution)
            return AgentTurn(execution.text, tool_details=[f"screen: {execution.text}"])

        # Compound browser/video workflow.  Bind the playback command to the
        # same tab we just activated.  This prevents Windows' current media
        # session (often VK/Yandex Music) from stealing «продолжи видео».
        video_tab_play = re.match(
            r"^(?:перейди|переключись|открой)\s+(?:на\s+)?вкладк\w*\s+(?:с\s+)?"
            r"(?:видео|ютуб\w*|youtube)\s+(?:и|затем|потом)\s+"
            r"(?:продолж\w*|возобнов\w*|включи|воспроизведи)\s+(?:воспроизведени\w*\s+)?(?:видео|ролик)?$",
            normalized,
        )
        if video_tab_play:
            browser_name = self._normalize_browser_argument("")
            tab_args = {"action": "activate_tab_title", "value": "youtube", "browser": browser_name}
            tab_execution = self.tools.execute("browser", tab_args)
            self._remember_execution_context("browser", tab_args, tab_execution)
            if not tab_execution.success:
                return AgentTurn(tab_execution.text, tool_details=[f"browser: {tab_execution.text}"])
            tab_data = tab_execution.data if isinstance(getattr(tab_execution, "data", None), dict) else {}
            tab_index = int(tab_data.get("index", 0) or 0)
            media_args = {"action": "play", "target": "video"}
            if tab_index:
                media_args.update({"tab_index": tab_index, "selected_tab_only": True})
                self._last_video_tab_index = tab_index
            media_execution = self.tools.execute("media", media_args)
            self._remember_execution_context("media", media_args, media_execution)
            return AgentTurn(
                f"{tab_execution.text}. {media_execution.text}",
                tool_details=[f"browser: {tab_execution.text}", f"media: {media_execution.text}"],
            )

        # Compound media command bound to an explicitly numbered tab.
        # The tab index itself is the identity. Do not infer a different service from
        # a possibly stale Firefox address-bar value in the Agent layer.
        ordinal_media = re.search(
            r"(?:(\d+)\s*(?:-?ю|-?ую|-?й|-?ей)?|(перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*))\s+вкладк\w*",
            normalized,
        )
        media_domain_now = self._media_semantic_domain(normalized)
        media_action_now = self._media_action_from_text(normalized)
        if ordinal_media and media_domain_now and media_action_now:
            word_numbers = {"перв": 1, "втор": 2, "трет": 3, "четвер": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}
            if ordinal_media.group(1):
                index = int(ordinal_media.group(1))
            else:
                token = str(ordinal_media.group(2) or "")
                index = next((num for stem, num in word_numbers.items() if token.startswith(stem)), 1)
            if media_domain_now == "music":
                # A tab number is irrelevant for music now. Do not even switch the
                # browser: the user's music command is executed in the desktop app.
                cfg = getattr(self, "config", None)
                preferred_music = str(cfg.get("desktop.preferred_music_app", "yandex_music") if cfg is not None else "yandex_music")
                args = {"action": media_action_now, "target": preferred_music}
                execution = self.tools.execute("media", args)
                self._remember_execution_context("media", args, execution)
                return AgentTurn(execution.text, tool_details=[f"media: {execution.text}"])
            browser_args = {"action": "activate_tab", "index": index, "browser": self._normalize_browser_argument(normalized)}
            tab_execution = self.tools.execute("browser", browser_args)
            self._remember_execution_context("browser", browser_args, tab_execution)
            if not tab_execution.success:
                return AgentTurn(tab_execution.text, tool_details=[f"browser: {tab_execution.text}"])
            self._last_video_tab_index = index
            media_args = {"action": media_action_now, "target": "youtube", "selected_tab_only": True, "tab_index": index}
            media_execution = self.tools.execute("media", media_args)
            self._remember_execution_context("media", media_args, media_execution)
            return AgentTurn(f"{tab_execution.text}. {media_execution.text}", tool_details=[f"browser: {tab_execution.text}", f"media: {media_execution.text}"])


        # Ordinal tab commands are deterministic and outrank window/title logic.
        # Accept filler-heavy word order from speech, e.g.
        # «в браузере мазила тебе нужно перейти на первую вкладку».
        # The actual browser is still pinned to Settings.
        ordinal_tab = re.search(
            r"(?:(\d+)\s*(?:-?ю|-?ую|-?й)?|"
            r"(перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*))\s+вкладк\w*",
            normalized,
        )
        ordinal_action = re.search(r"\b(?:открой|открыть|переключись|переключиться|перейди|перейти|покажи|показать|выбери|выбрать)\w*\b", normalized)
        ordinal_close = re.search(r"\b(?:закрой|закрыть)\w*\b", normalized)
        if ordinal_tab and (ordinal_action or ordinal_close):
            word_numbers = {"перв": 1, "втор": 2, "трет": 3, "четвер": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}
            if ordinal_tab.group(1):
                index = int(ordinal_tab.group(1))
            else:
                token = str(ordinal_tab.group(2) or "")
                index = next((num for stem, num in word_numbers.items() if token.startswith(stem)), 1)
            action = "close_tab" if ordinal_close else "activate_tab"
            args = {"action": action, "index": index, "browser": self._normalize_browser_argument(normalized)}
            execution = self.tools.execute("browser", args)
            self._remember_execution_context("browser", args, execution)
            return AgentTurn(execution.text, tool_details=[f"browser: {execution.text}"])

        # Universal accessibility control for arbitrary Windows applications.
        # These commands operate on the *foreground* window and identify controls
        # by accessible name/role, so the user does not need coordinates or a
        # dedicated integration for every program.
        list_controls = re.match(
            r"^(?:покажи|перечисли|какие)\s+(?:доступн\w*\s+)?(?:элемент\w*|кнопк\w*)(?:\s+(?:есть\s+)?(?:в\s+)?(?:этом|текущем|активном)\s+окне)?$",
            normalized,
        )
        if list_controls:
            role = "Button" if "кноп" in normalized else ""
            args = {"action": "list_controls"}
            if role:
                args["control_type"] = role
            execution = self.tools.execute("input_control", args)
            return AgentTurn(execution.text, tool_details=[f"input_control: {execution.text}"])

        click_named_control = re.match(
            r'^(?:нажми|кликни|щелкни)\s+(?:на\s+)?(?:(кнопк\w*|ссылк\w*|пункт\w*\s+меню|вкладк\w*|чекбокс|флажок)\s+)?[«"]?(.+?)[»"]?$',
            normalized,
        )
        if click_named_control:
            role_word = str(click_named_control.group(1) or "")
            name = str(click_named_control.group(2) or "").strip(' "«»')
            # A raw keyboard-key command such as “нажми Enter” should continue to
            # the hotkey/key routing instead of searching UIA for a button called Enter.
            keyboard_names = {"enter", "энтер", "escape", "esc", "tab", "space", "пробел", "delete", "backspace", "home", "end"}
            looks_like_hotkey = bool(re.match(r"^(?:ctrl|control|контрол|контроль|shift|шифт|alt|альт|win|windows)\b", normalize_target(name)))
            if role_word or (normalize_target(name) not in keyboard_names and not looks_like_hotkey):
                role_map = {
                    "кноп": "Button", "ссыл": "Hyperlink", "пункт": "MenuItem",
                    "вклад": "TabItem", "чекбокс": "CheckBox", "флаж": "CheckBox",
                }
                control_type = next((value for stem, value in role_map.items() if stem in role_word), "")
                args = {"action": "click_control", "name": name}
                if control_type:
                    args["control_type"] = control_type
                execution = self.tools.execute("input_control", args)
                return AgentTurn(execution.text, tool_details=[f"input_control: {execution.text}"])

        press_keys = re.match(r"^(?:нажми|зажми|press)\s+(?:клавиш\w*\s+|сочетани\w*\s+)?(.+)$", normalized)
        if press_keys:
            raw_keys = str(press_keys.group(1) or "").strip()
            # Do not steal semantic UIA commands such as “нажми кнопку Сохранить”.
            if not re.match(r"^(?:кнопк|ссылк|пункт\w*\s+меню|вкладк|чекбокс|флажок)\b", raw_keys):
                key_aliases = {
                    "control": "ctrl", "контрол": "ctrl", "контроль": "ctrl", "ctrl": "ctrl",
                    "shift": "shift", "шифт": "shift", "alt": "alt", "альт": "alt",
                    "win": "win", "windows": "win", "вин": "win",
                    "enter": "enter", "энтер": "enter", "ввод": "enter",
                    "escape": "esc", "esc": "esc", "эскейп": "esc",
                    "tab": "tab", "таб": "tab", "space": "space", "пробел": "space",
                    "delete": "delete", "делит": "delete", "backspace": "backspace", "бэкспейс": "backspace",
                    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
                    "вверх": "up", "вниз": "down", "влево": "left", "вправо": "right",
                }
                compact = raw_keys.replace("+", " ").replace(" плюс ", " ")
                tokens = [t for t in re.split(r"\s+", compact) if t]
                keys: list[str] = []
                for token in tokens:
                    key = key_aliases.get(token, token)
                    if re.fullmatch(r"f(?:[1-9]|1[0-2])", key) or re.fullmatch(r"[a-z0-9]", key) or key in set(key_aliases.values()):
                        keys.append(key)
                if keys and len(keys) == len(tokens):
                    args = {"action": "hotkey", "keys": keys}
                    execution = self.tools.execute("input_control", args)
                    return AgentTurn(execution.text, tool_details=[f"input_control: {execution.text}"])

        type_literal = re.match(r'^(?:напечатай|введи)\s+(?:текст\s+)?[«"]?(.+?)[»"]?$', normalized)
        if type_literal and not re.match(r"^(?:в\s+)?поле\b", str(type_literal.group(1) or "")):
            text_value = str(type_literal.group(1) or "").strip(' "«»')
            if text_value:
                args = {"action": "type_text", "text": text_value}
                execution = self.tools.execute("input_control", args)
                return AgentTurn(execution.text, tool_details=[f"input_control: {execution.text}"])

        focus_named_field = re.match(
            r'^(?:выбери|активируй|перейди\s+в|поставь\s+курсор\s+в)\s+(?:поле|поле\s+ввода)\s+[«"]?(.+?)[»"]?$',
            normalized,
        )
        if focus_named_field:
            args = {"action": "focus_control", "name": focus_named_field.group(1).strip(' "«»'), "control_type": "Edit"}
            execution = self.tools.execute("input_control", args)
            return AgentTurn(execution.text, tool_details=[f"input_control: {execution.text}"])

        set_named_field = re.match(
            r'^(?:введи|впиши|напиши)\s+(?:в\s+)?(?:поле|поле\s+ввода)\s+[«"]?(.+?)[»"]?\s+(?:текст|значение)\s+[«"]?(.+?)[»"]?$',
            normalized,
        )
        if not set_named_field:
            set_named_field = re.match(
                r'^(?:в\s+)?(?:поле|поле\s+ввода)\s+[«"]?(.+?)[»"]?\s+(?:введи|впиши|напиши)\s+[«"]?(.+?)[»"]?$',
                normalized,
            )
        if set_named_field:
            args = {
                "action": "set_control_text",
                "name": set_named_field.group(1).strip(' "«»'),
                "text": set_named_field.group(2).strip(' "«»'),
                "control_type": "Edit",
            }
            execution = self.tools.execute("input_control", args)
            return AgentTurn(execution.text, tool_details=[f"input_control: {execution.text}"])

        # Named browser tabs/services: “перейди на вкладку VK”, “открой вкладку с Telegram”.
        named_tab = re.match(
            r"^(?:открой|переключись\s+на|перейди\s+на|покажи)\s+вкладк\w*\s+(?:с\s+)?(.+)$",
            normalized,
        )
        if named_tab and not re.match(r"^(?:\d+|перв|втор|трет|четвер|пят|шест|седьм|восьм|девят|десят)", named_tab.group(1)):
            query = named_tab.group(1).strip()
            if re.fullmatch(r"(?:видео|ролик|видеоплеер)", query):
                query = "youtube"
            execution = self.tools.execute("browser", {"action": "activate_tab_title", "value": query, "browser": self._normalize_browser_argument(normalized)})
            self._remember_execution_context("browser", {"action": "activate_tab_title", "value": query}, execution)
            return AgentTurn(execution.text, tool_details=[f"browser: {execution.text}"])

        # Browser tab control by ordinal position, left-to-right. A browser name
        # may follow the command: «открой вторую вкладку в браузере мозила».
        tab_open = re.match(
            r"^(?:открой|переключись\s+на|перейди\s+на|покажи)\s+(?:(\d+)\s*(?:-?ю|-?ую|-?й)?|"
            r"(перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*))\s+вкладк\w*"
            r"(?:\s+(?:в\s+)?(?:браузер\w*\s+)?(.+))?$",
            normalized,
        )
        if tab_open:
            word_numbers = {"перв": 1, "втор": 2, "трет": 3, "четвер": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}
            if tab_open.group(1):
                index = int(tab_open.group(1))
            else:
                token = str(tab_open.group(2) or "")
                index = next((num for stem, num in word_numbers.items() if token.startswith(stem)), 1)
            browser_name = self._normalize_browser_argument(str(tab_open.group(3) or ""))
            execution = self.tools.execute("browser", {"action": "activate_tab", "index": index, "browser": browser_name})
            return AgentTurn(execution.text, tool_details=[f"browser: {execution.text}"])

        tab_close = re.match(
            r"^закрой\s+(?:(\d+)\s*(?:-?ю|-?ую|-?й)?|"
            r"(перв\w*|втор\w*|трет\w*|четвер\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*))\s+вкладк\w*"
            r"(?:\s+(?:в\s+)?(?:браузер\w*\s+)?(.+))?$",
            normalized,
        )
        if tab_close:
            word_numbers = {"перв": 1, "втор": 2, "трет": 3, "четвер": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}
            if tab_close.group(1):
                index = int(tab_close.group(1))
            else:
                token = str(tab_close.group(2) or "")
                index = next((num for stem, num in word_numbers.items() if token.startswith(stem)), 1)
            browser_name = self._normalize_browser_argument(str(tab_close.group(3) or ""))
            execution = self.tools.execute("browser", {"action": "close_tab", "index": index, "browser": browser_name})
            return AgentTurn(execution.text, tool_details=[f"browser: {execution.text}"])

        # Internet-search language can contain filler words: "найди информацию в
        # интернете про березу", "поищи в сети что такое X", etc.
        browser_lookup = re.match(r"^(?:найди|поищи|отыщи)\s+(?:мне\s+)?(?:в\s+браузер\w*|через\s+браузер\w*)\s+(.+)$", normalized)
        if browser_lookup:
            query = browser_lookup.group(1).strip(" ,.-")
            if query:
                execution = self.tools.execute("browser", {"action": "search", "value": query})
                return AgentTurn(execution.text, tool_details=[f"browser: {execution.text}"])

        # Absolute master volume. Allow natural qualifiers between the object and
        # percentage: "громкость музыки в браузере 80%".
        if re.match(r"^(?:сделай|установи|поставь|выставь)", normalized) and "громк" in normalized:
            numbers = re.findall(r"(?<!\d)(\d{1,3})(?:\s*%|\s*процент\w*)?", normalized)
            if numbers:
                value = max(0, min(100, int(numbers[-1])))
                audio_target = ""
                volume_domain = self._media_semantic_domain(normalized)
                if volume_domain == "music":
                    cfg = getattr(self, "config", None)
                    audio_target = str(cfg.get("desktop.preferred_music_app", "yandex_music") if cfg is not None else "yandex_music")
                elif volume_domain == "video":
                    audio_target = "youtube"
                elif "ютуб" in normalized or "youtube" in normalized:
                    audio_target = "youtube"
                elif re.search(r"\b(?:вк|vk)\b|вконтакт", normalized):
                    audio_target = "вконтакте"
                elif "яндекс" in normalized and ("музык" in normalized or "music" in normalized):
                    cfg = getattr(self, "config", None)
                    audio_target = str(cfg.get("desktop.preferred_music_app", "yandex_music") if cfg is not None else "yandex_music")
                elif "браузер" in normalized:
                    audio_target = "browser"
                args = {"action": "set_volume", "value": value}
                if audio_target:
                    args["target"] = audio_target
                execution = self.tools.execute("audio", args)
                return AgentTurn(execution.text, tool_details=[f"audio: {execution.text}"])

        if normalized in {"выключи звук", "убери звук", "отключи звук", "замьюти звук"}:
            execution = self.tools.execute("audio", {"action": "mute", "muted": True})
            return AgentTurn(execution.text, tool_details=[f"audio: {execution.text}"])
        if normalized in {"включи звук", "верни звук", "размьюти звук"}:
            execution = self.tools.execute("audio", {"action": "mute", "muted": False})
            return AgentTurn(execution.text, tool_details=[f"audio: {execution.text}"])

        target = media_target(normalized)
        shorts = re.search(r"(?:shorts|шортс|шорт\w*)", normalized)
        if shorts:
            direction = ""
            if re.search(r"следующ|вперед|вперёд|дальше|листай\s+вперед|пролист", normalized):
                direction = "shorts_next"
            if re.search(r"предыдущ|назад|обратно|верни", normalized):
                direction = "shorts_previous"
            if direction:
                args = {"action": direction, "target": "youtube"}
                execution = self.tools.execute("media", args)
                self._remember_execution_context("media", args, execution)
                return AgentTurn(execution.text, tool_details=[f"media: {execution.text}"])
        # Explicit relative seeking goes before generic play/pause routing.
        # Accept natural word order: “перемотай видео на 30 секунд вперед”,
        # “перемотай вперед на минуту”, “мотни на 50 секунд назад”.
        seek_phrase = re.search(
            r"(?:перемотай|перемотать|мотни|отмотай)\s+(?:видео\s+|ролик\s+|плеер\s+)?"
            r"(?:(вперед|назад)\s+)?(?:на\s+)?"
            r"(пол\s*минуты|полминуты|минуту|минута|минуты|секунду|секунда|\d+\s*(?:секунд\w*|сек\.?|минут\w*|мин\.?|минута|минуту)|"
            r"(?:одну|один|две|два|три|четыре|пять)\s+(?:секунд\w*|минут\w*))"
            r"(?:\s+(вперед|назад))?",
            normalized, flags=re.I,
        )
        if seek_phrase:
            direction = str(seek_phrase.group(1) or seek_phrase.group(3) or "вперед").casefold()
            amount = str(seek_phrase.group(2) or "").casefold().replace("ё", "е").strip()
            words_to_num = {"одну": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5}
            if "пол" in amount and "минут" in amount:
                seconds = 30
            else:
                m_num = re.search(r"\d+", amount)
                number = int(m_num.group(0)) if m_num else next((value for word, value in words_to_num.items() if re.search(rf"\b{word}\b", amount)), 1)
                seconds = number * 60 if "мин" in amount else number
            seconds = max(1, min(3600, int(seconds)))
            action = "seek_forward" if direction == "вперед" else "seek_backward"
            seek_target = "youtube" if re.search(r"\b(?:видео|ролик)\b", normalized, flags=re.I) else target
            args = {"action": action, "seconds": seconds, "target": seek_target}
            if seek_target == "youtube":
                remembered_index = int(getattr(self, "_last_video_tab_index", 0) or 0)
                if remembered_index:
                    args.update({"tab_index": remembered_index, "selected_tab_only": True})
            execution = self.tools.execute("media", args)
            self._remember_execution_context("media", args, execution)
            return AgentTurn(execution.text, tool_details=[f"media: {execution.text}"])

        media_object = bool(re.search(r"музык|песн|трек|видео|воспроизвед|плеер|playback", normalized))

        # Explicit video pause must not depend on the generic phrase order. The
        # common voice form is “поставь видео на паузу”, where the object sits
        # between the verb and “на паузу”. Route it directly to YouTube/video and
        # preserve an exact remembered browser tab when one is known.
        if re.match(
            r"^(?:поставь\s+(?:видео|ролик|ютуб|youtube)\s+на\s+паузу|"
            r"(?:приостанови|останови)\s+(?:видео|ролик|ютуб|youtube)|"
            r"(?:видео|ролик|ютуб|youtube)\s+(?:на\s+паузу|поставь\s+на\s+паузу))$",
            normalized, flags=re.I,
        ):
            args = {"action": "pause", "target": "youtube"}
            remembered_index = int(getattr(self, "_last_video_tab_index", 0) or 0)
            if remembered_index:
                args.update({"tab_index": remembered_index, "selected_tab_only": True})
            execution = self.tools.execute("media", args)
            self._remember_execution_context("media", args, execution)
            return AgentTurn(execution.text, tool_details=[f"media: {execution.text}"])

        # v3.9 semantic boundary: a concrete track search requires an explicit
        # search verb + musical object. “Поставь трек на паузу” is therefore a
        # state command, not a lookup for a song called “на паузу”.  Native Yandex
        # integration normally consumes this earlier; this is a defensive fallback.
        track_match = re.match(
            r"^(?:найди|поищи|отыщи)\s+(?:трек|песн\w*|композици\w*|музык\w*)\s+(.+?)(?:\s+и\s+(?:включи|запусти|воспроизведи)(?:\s+(?:ее|её|его))?)?$",
            normalized,
        )
        if track_match:
            query = track_match.group(1).strip()
            service_target = target or "music"
            query = re.sub(
                r"\s+(?:(?:в|во|на)\s+)?(?:вкладк\w*\s+)?(?:в|во|на)?\s*"
                r"(?:вк|vk|вконтакте|яндекс(?:\s+музык\w*)?|youtube|ютуб)\s*$",
                "", query, flags=re.I,
            ).strip()
            play_requested = bool(re.search(r"\s+и\s+(?:включи|запусти|воспроизведи)(?:\s+(?:ее|её|его))?$", normalized))
            if not play_requested:
                # Searching must not start playback, including the legacy path.
                if service_target not in {"music", "spotify", "yandex", "yandex_music"}:
                    from jarvis.runtime.dialogue_messages import message
                    from jarvis.language import language_code
                    return AgentTurn(message('track_request', language_code(self.config)), outcome='clarification')
                tool = 'yandex_music' if service_target in {'yandex', 'yandex_music'} else service_target
                args = {"action": "search_track", "query": query}
            else:
                tool, args = "media", {"action": "play_track", "target": service_target, "query": query}
            execution = self.tools.execute(tool, args)
            self._remember_execution_context(tool, args, execution)
            return AgentTurn(execution.text, tool_details=[f"{tool}: {execution.text}"])

        # Keep the user's requested semantic boundary explicit.  “Включи песню X”
        # does not silently become a search and also does not accidentally resume
        # whatever happens to be playing.  The assistant tells the shortest form
        # that unambiguously means a lookup.
        ambiguous_track_lookup = re.match(
            r"^(?:включи|поставь|запусти|вруби)\s+(?:трек|песн\w*|композици\w*|музык\w*)\s+(.+)$",
            normalized, flags=re.I,
        )
        if ambiguous_track_lookup:
            tail = str(ambiguous_track_lookup.group(1) or "").strip()
            if not re.fullmatch(r"(?:на\s+паузу|паузу|снова|дальше)", tail, flags=re.I):
                from jarvis.runtime.dialogue_messages import message
                from jarvis.language import language_code
                return AgentTurn(message('track_request', language_code(self.config)), outcome='clarification')

        if re.search(r"(?:что|какая|какой).*(?:играет|воспроизвод)", normalized):
            # Status is domain-aware. v4.0.11 incorrectly sent a target-less music
            # question to the YouTube-only media tool, producing the confusing
            # "Media tool управляет только YouTube" response. Prefer the last
            # confirmed owner, then inspect headless Yandex/Discord state without
            # opening or focusing any application.
            remembered = str(getattr(self, "_last_media_target", "") or "").strip().casefold()
            explicit_domain = self._media_semantic_domain(normalized)
            if explicit_domain == "video" or remembered == "youtube":
                execution = self.tools.execute("media", {"action": "status", "target": "youtube"})
                self._remember_execution_context("media", {"action": "status", "target": "youtube"}, execution)
                return AgentTurn(execution.text, tool_details=[f"media: {execution.text}"])
            if explicit_domain == "music" and re.search(r"\b(?:discord|дискорд)\b", normalized):
                remembered = "discord"
            if remembered == "discord":
                args = {"action": "music_queue_status"}
                if getattr(self, "_last_discord_server", ""):
                    args["server"] = self._last_discord_server
                execution = self.tools.execute("discord", args)
                self._remember_execution_context("discord", args, execution)
                return AgentTurn(execution.text, tool_details=[f"discord: {execution.text}"])
            if remembered in {"yandex_music", "music"}:
                execution = self.tools.execute("yandex_music", {"action": "status"})
                self._remember_execution_context("yandex_music", {"action": "status"}, execution)
                return AgentTurn(execution.text, tool_details=[f"yandex_music: {execution.text}"])

            # No remembered owner: query only non-invasive background state.
            yandex = self.tools.execute("yandex_music", {"action": "status"})
            ydata = dict(getattr(yandex, "data", {}) or {})
            if bool(ydata.get("playing")) or str(ydata.get("state", "") or "").casefold() in {"playing", "paused"}:
                self._remember_execution_context("yandex_music", {"action": "status"}, yandex)
                return AgentTurn(yandex.text, tool_details=[f"yandex_music: {yandex.text}"])
            args = {"action": "music_queue_status"}
            if getattr(self, "_last_discord_server", ""):
                args["server"] = self._last_discord_server
            discord = self.tools.execute("discord", args)
            ddata = dict(getattr(discord, "data", {}) or {})
            if ddata.get("current"):
                self._remember_execution_context("discord", args, discord)
                return AgentTurn(discord.text, tool_details=[f"discord: {discord.text}"])
            return AgentTurn("Сейчас NEO не видит активного фонового трека в Яндекс Музыке или Discord. Для YouTube уточни: «что сейчас играет в YouTube»." )

        if media_object:
            action = ""
            if (
                re.search(r"\b(?:пауза|стоп|приостанови|останови|остановить|поставь на паузу)\b", normalized)
                or re.search(r"\bпоставь\s+(?:текущ(?:ую|ий)\s+)?(?:музык\w*|песн\w*|трек\w*|видео\w*|ролик\w*)\s+на\s+паузу\b", normalized)
            ):
                action = "pause"
            elif "следующ" in normalized or "next" in normalized:
                action = "next"
            elif "предыдущ" in normalized or "previous" in normalized:
                action = "previous"
            elif re.search(r"\b(?:продолж\w*|возобнов\w*|resume|воспроизведи|доиграй)\b", normalized):
                action = "play"
            elif re.search(r"\b(?:включи|play)\b", normalized) and not normalized.startswith(("открой ", "запусти яндекс")):
                action = "play"
            if action:
                args = {"action": action, "target": target}
                domain_now = self._media_semantic_domain(normalized)
                remembered_index = int(getattr(self, "_last_video_tab_index", 0) or 0) if domain_now == "video" else 0
                if remembered_index:
                    args.update({"tab_index": remembered_index, "selected_tab_only": True, "target": "youtube"})
                execution = self.tools.execute("media", args)
                self._remember_execution_context("media", args, execution)
                return AgentTurn(execution.text, tool_details=[f"media: {execution.text}"])

        # Short standalone media commands: ask the real background owner first.
        # This closes the path where ASR "следующий трап" used to miss Discord
        # ownership and fall through to a different media backend.
        contextual_media = self._try_contextual_discord_music_control(normalized)
        if contextual_media is not None:
            return contextual_media

        # Short standalone media commands.
        media_commands = {
            "пауза": "pause", "поставь на паузу": "pause", "продолжи": "play",
            "следующий трек": "next", "следующая песня": "next", "переключи трек": "next",
            "предыдущий трек": "previous", "предыдущая песня": "previous",
            "play pause": "play_pause", "pause": "pause", "resume": "play",
            "next track": "next", "previous track": "previous",
        }
        if normalized in media_commands:
            action = media_commands[normalized]
            args = {"action": action, "target": target}
            domain_now = self._media_semantic_domain(normalized)
            # Final v2 media split: a remembered browser tab is meaningful only for
            # YouTube/video. Music must never inherit any historical browser-tab
            # state from older sessions/builds.
            remembered_index = int(getattr(self, "_last_video_tab_index", 0) or 0) if domain_now == "video" else 0
            if remembered_index:
                args.update({"tab_index": remembered_index, "selected_tab_only": True, "target": "youtube"})
            execution = self.tools.execute("media", args)
            self._remember_execution_context("media", args, execution)
            return AgentTurn(execution.text, tool_details=[f"media: {execution.text}"])

        # Core disk/folder commands are deterministic so accessibility workflows
        # do not depend on the local LLM guessing a path or a tool schema.
        if re.match(r"^(?:открой|покажи|перечисли|какие)\s+(?:мне\s+)?(?:диски|тома)(?:\s+(?:есть|доступны))?$", normalized):
            execution = self.tools.execute("filesystem", {"action": "list_drives"})
            return AgentTurn(execution.text if not execution.success else "\n".join(
                f"{item.get('mount','?')}: свободно {round(float(item.get('free',0))/(1024**3),1)} ГБ из {round(float(item.get('total',0))/(1024**3),1)} ГБ"
                for item in (execution.data or []) if isinstance(item, dict) and item.get('total')
            ) or execution.text, tool_details=[f"filesystem: {execution.text}"])

        disk_space = re.match(r"^(?:сколько|покажи)\s+(?:свободного\s+)?места(?:\s+осталось)?\s+(?:на\s+)?(?:диск\w*\s+)?([a-zа-я])(?::)?$", normalized, flags=re.I)
        if disk_space:
            letter = disk_space.group(1).upper()
            source = f"{letter}:\\" if "A" <= letter <= "Z" else ""
            if source:
                execution = self.tools.execute("filesystem", {"action": "disk_usage", "source": source})
                return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        open_drive = re.match(r"^(?:открой|покажи)\s+диск\w*\s+([a-z])(?::)?$", normalized, flags=re.I)
        if open_drive:
            source = f"{open_drive.group(1).upper()}:\\"
            execution = self.tools.execute("filesystem", {"action": "open", "source": source})
            self._remember_execution_context("filesystem", {"action": "open", "source": source}, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        if re.match(r"^(?:покажи|перечисли|что\s+(?:есть|находится))\s+(?:в\s+)?(?:этой|текущей)\s+папке$", normalized) and self._last_folder_path:
            args = {"action": "list", "source": self._last_folder_path, "limit": 100}
            execution = self.tools.execute("filesystem", args)
            if execution.success and isinstance(execution.data, list):
                names = [str(item.get("name", "")) for item in execution.data if isinstance(item, dict)]
                return AgentTurn("В папке: " + (", ".join(names[:30]) if names else "пусто"), tool_details=[f"filesystem: {execution.text}"])
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        if re.match(r"^(?:покажи|выведи|построй)\s+(?:мне\s+)?дерево\s+(?:этой|текущей)\s+папк\w*$", normalized) and self._last_folder_path:
            args = {"action": "tree", "source": self._last_folder_path, "depth": 2, "limit": 250}
            execution = self.tools.execute("filesystem", args)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        if re.match(r"^(?:покажи|дай)\s+(?:мне\s+)?(?:информац\w*|сведени\w*)\s+(?:об|о)\s+(?:этом|текущем)\s+файл\w*$", normalized) and self._last_file_path:
            args = {"action": "info", "source": self._last_file_path}
            execution = self.tools.execute("filesystem", args)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        if re.match(r"^(?:посчитай|покажи|дай)\s+(?:мне\s+)?(?:sha\s*256|sha256|хеш|hash)(?:\s+(?:этого|текущего))?\s+файл\w*$", normalized) and self._last_file_path:
            args = {"action": "hash", "source": self._last_file_path, "algorithm": "sha256"}
            execution = self.tools.execute("filesystem", args)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        create_folder_current = re.match(r"^(?:в\s+(?:этой|текущей)\s+папке\s+)?создай\s+(?:новую\s+)?папк\w*\s+(.+)$", normalized)
        if create_folder_current and self._last_folder_path:
            from pathlib import Path as _DirPath
            name = create_folder_current.group(1).strip(' "«».,;')
            args = {"action": "create_folder", "destination": str(_DirPath(self._last_folder_path) / name)}
            execution = self.tools.execute("filesystem", args)
            self._remember_execution_context("filesystem", args, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # Context-aware file operations for hands-free workflows. These run before
        # the LLM so references such as «этот файл» cannot turn into a global search.
        search_in_current = re.match(r"^(?:найди|отыщи)\s+(?:в\s+)?(?:этом|этом\s+открытом|текущем)?\s*файл\w*\s+(.+)$", normalized)
        if search_in_current:
            target_path = self._resolve_context_file("этот файл")
            if target_path:
                args = {"action": "search_text", "source": target_path, "query": search_in_current.group(1).strip()}
                execution = self.tools.execute("filesystem", args)
                self._remember_execution_context("filesystem", args, execution)
                return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        read_current = re.match(r"^(?:прочитай|покажи\s+содержимое|что\s+(?:написано|находится))\s+(?:в\s+)?(?:этом\s+)?файл\w*$", normalized)
        if read_current:
            target_path = self._resolve_context_file("этот файл")
            if target_path:
                args = {"action": "read_text", "source": target_path}
                execution = self.tools.execute("filesystem", args)
                self._remember_execution_context("filesystem", args, execution)
                return AgentTurn(execution.text if not execution.success else str(execution.data), tool_details=[f"filesystem: {execution.text}"])

        rename_current = re.match(r"^переименуй\s+(его|этот\s+файл|файл|эту\s+папку|папку)\s+(?:в|как)\s+(.+)$", normalized)
        if rename_current:
            is_folder = "папк" in rename_current.group(1)
            current = self._last_folder_path if is_folder else (self._resolve_context_file("этот открытый файл") or self._last_file_path)
            if current:
                new_name = rename_current.group(2).strip(' "«».,;') if is_folder else self._normalize_spoken_filename(rename_current.group(2))
                args = {"action": "rename", "source": current, "new_name": new_name}
                execution = self.tools.execute("filesystem", args)
                self._remember_execution_context("filesystem", args, execution)
                return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        copy_current = re.match(r"^скопируй\s+(его|этот\s+файл|файл|эту\s+папку|папку)\s+(?:в|в\s+папку)\s+(.+)$", normalized)
        if copy_current:
            is_folder = "папк" in copy_current.group(1)
            current = self._last_folder_path if is_folder else (self._resolve_context_file("этот открытый файл") or self._last_file_path)
            if current:
                dest_res = self.tools.execute("desktop", {"action": "resolve", "query": copy_current.group(2).strip(), "hint": "folder"})
                if dest_res.success and isinstance(dest_res.data, dict):
                    from pathlib import Path as _CopyPath
                    folder = str(dest_res.data.get("value", "") or "")
                    args = {"action": "copy", "source": current, "destination": str(_CopyPath(folder) / _CopyPath(current).name)}
                    execution = self.tools.execute("filesystem", args)
                    self._remember_execution_context("filesystem", args, execution)
                    return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        zip_current = re.match(r"^(?:заархивируй|архивируй|создай\s+архив\s+из)\s+(?:эту\s+)?(?:папк\w*|файл\w*|его)?(?:\s+в\s+(.+))?$", normalized)
        if zip_current:
            src = self._last_file_path or self._last_folder_path
            if src:
                args = {"action": "zip", "source": src}
                if zip_current.group(1): args["destination"] = zip_current.group(1).strip()
                execution = self.tools.execute("filesystem", args)
                self._remember_execution_context("filesystem", args, execution)
                return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # Common editor actions should work directly on the currently focused text
        # editor. These are deterministic and do not need the LLM.
        if re.match(r"^(?:сохрани|save)(?:\s+(?:этот\s+)?(?:файл|документ))?$", normalized):
            execution = self.tools.execute("input_control", {"action": "hotkey", "keys": ["ctrl", "s"]})
            return AgentTurn("Файл сохранён." if execution.success else execution.text, tool_details=[f"input_control: {execution.text}"])
        if re.match(r"^(?:отмени|undo)(?:\s+последн\w+\s+изменен\w*)?(?:\s+в\s+файле)?$", normalized):
            execution = self.tools.execute("filesystem", {"action": "undo_last_change"})
            if execution.success:
                self._remember_execution_context("filesystem", {"action": "undo_last_change"}, execution)
                return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])
            # If there is no JARVIS file mutation to restore, fall back to the
            # active editor's own undo stack.
            fallback = self.tools.execute("input_control", {"action": "hotkey", "keys": ["ctrl", "z"]})
            return AgentTurn("Отправил отмену в активное приложение.", tool_details=[f"input_control: {fallback.text}"])

        replace_current = re.match(
            r"^(?:в\s+(?:этом\s+)?файле\s+)?замени\s+[\"«]?(.+?)[\"»]?\s+на\s+[\"«]?(.+?)[\"»]?$",
            normalized,
        )
        if replace_current:
            target_path = self._resolve_context_file("этот файл")
            if not target_path:
                return AgentTurn("Не могу однозначно определить открытый файл. Открой нужный документ или назови его.")
            old_text = replace_current.group(1).strip(' \"«»')
            new_text = replace_current.group(2).strip(' \"«»')
            arguments = {
                "action": "replace_text",
                "source": target_path,
                "old_text": old_text,
                "content": new_text,
                "count": 1,
            }
            execution = self.tools.execute("filesystem", arguments)
            self._remember_execution_context("filesystem", arguments, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # Direct named-file write: «напиши/запиши в test.txt 123».  Resolve the
        # file in the current folder first; do not let the LLM accidentally turn
        # trailing words into part of the filename.
        direct_named_write = re.match(
            r"^(?:запиши|напиши|добавь)\s+в\s+([\w .()\-]+?\.[a-zа-я0-9]{1,10})\s+(.+)$",
            normalized,
            flags=re.I,
        )
        if direct_named_write:
            file_query = direct_named_write.group(1).strip(' \"«»')
            content = direct_named_write.group(2).strip()
            target_path = self._resolve_context_file(file_query)
            if not target_path:
                return AgentTurn(
                    f"Не могу безопасно определить, какой именно «{file_query}» нужно изменить. "
                    "Открой нужный файл или назови папку — для записи я не выбираю похожий файл по всему диску."
                )
            arguments = {"action": "write_text", "source": target_path, "content": content, "append": True}
            execution = self.tools.execute("filesystem", arguments)
            self._remember_execution_context("filesystem", arguments, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # Context-aware editing of a text file that is already open/created.
        # Examples: "test.txt и внеси в него с новой строки текст 12345" or
        # "добавь в него новую строку hello" after a previous file operation.
        named_edit = re.match(
            r"^(.+?\.[a-zа-я0-9]{1,8})\s+(?:и\s+)?(?:внеси|добавь|запиши|напиши)\s+"
            r"(?:в\s+(?:него|файл)\s+)?(?:(с\s+новой\s+строки)\s+)?(?:текст\s+)?(.+)$",
            normalized,
        )
        contextual_edit = re.match(
            r"^(?:и\s+)?(?:внеси|добавь|запиши|напиши)\s+(?:в\s+(?:него|этот\s+файл(?:\s+который\s+ты\s+открыл)?|файл)\s+)?"
            r"(?:(с\s+новой\s+строки|новую\s+строку)\s+)?(?:текст\s+)?(.+)$",
            normalized,
        )
        if named_edit or contextual_edit:
            if named_edit:
                file_query = named_edit.group(1).strip()
                newline_requested = bool(named_edit.group(2))
                content = named_edit.group(3).strip()
                target_path = self._resolve_context_file(file_query)
                if not target_path:
                    return AgentTurn(
                        f"Не могу безопасно определить файл «{file_query}» для изменения. "
                        "Открой его или скажи, в какой он папке."
                    )
            else:
                target_path = self._resolve_context_file("этот открытый файл") or str(getattr(self, "_last_file_path", "") or "")
                newline_requested = bool(contextual_edit.group(1))
                content = contextual_edit.group(2).strip()
            if not target_path:
                from jarvis.runtime.dialogue_messages import message
                from jarvis.language import language_code
                return AgentTurn(message('file_to_modify', language_code(self.config)), outcome='clarification')
            payload = ("\n" if newline_requested else "") + content
            arguments = {"action": "write_text", "source": target_path, "content": payload, "append": True}
            execution = self.tools.execute("filesystem", arguments)
            self._remember_execution_context("filesystem", arguments, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # Deterministic «создай test.txt и открой его».  Older builds let the
        # trailing words become part of the filename ("test.txt и открой его.txt").
        create_and_open = re.match(
            r"^(?:в\s+(?:этой|текущей)\s+папке\s+)?создай\s+"
            r"(?:(txt|текстов\w*|markdown|md|json|csv)\s+)?(?:файл\s+)?"
            r"([^,;]+?)(?:\s+(?:и|затем|потом)\s+)(?:открой|запусти)\s+(?:его|этот\s+файл|файл)$",
            normalized,
        )
        if create_and_open and getattr(self, "_last_folder_path", ""):
            file_name = created_filename(create_and_open.group(2), create_and_open.group(1) or "")
            arguments = {
                "action": "create_text_file",
                "folder": getattr(self, "_last_folder_path", ""),
                "name": file_name,
                "content": "",
            }
            created = self.tools.execute("filesystem", arguments)
            self._remember_execution_context("filesystem", arguments, created)
            if not created.success:
                return AgentTurn(created.text, tool_details=[f"filesystem: {created.text}"])
            created_path = str((created.data or {}).get("path", "") if isinstance(created.data, dict) else "")
            if created_path:
                open_args = {"action": "open", "source": created_path}
                opened = self.tools.execute("filesystem", open_args)
                self._remember_execution_context("filesystem", open_args, opened)
                return AgentTurn(f"{created.text}. {opened.text}", tool_details=[f"filesystem: {created.text}", f"filesystem: {opened.text}"])
            return AgentTurn(created.text, tool_details=[f"filesystem: {created.text}"])

        # Standalone creation inside the folder established by the previous
        # command / foreground Explorer window. Supports: «создай файл test.txt»,
        # «в этой папке создай markdown файл notes.md и запиши ...».
        create_current = re.match(
            r"^(?:в\s+(?:этой|текущей)\s+папке\s+)?создай\s+"
            r"(?:(txt|текстов\w*|markdown|md|json|csv)\s+)?файл(?:\s+с\s+названи\w*)?\s+"
            r"([^,;]+?)(?:\s+(?:и\s+)?(?:запиши|напиши|впиши)\s+(?:в\s+(?:него|файл)\s+)?(.+))?$",
            normalized,
        )
        if create_current and getattr(self, "_last_folder_path", ""):
            file_name = created_filename(create_current.group(2), create_current.group(1) or "")
            content = str(create_current.group(3) or "")
            arguments = {
                "action": "create_text_file",
                "folder": getattr(self, "_last_folder_path", ""),
                "name": file_name,
                "content": content,
            }
            execution = self.tools.execute("filesystem", arguments)
            self._remember_execution_context("filesystem", arguments, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        create_named_folder = re.match(
            r"^(?:в\s+)?папк\w*\s+(.+?)\s+создай\s+(?:(txt|текстов\w*|markdown|md|json|csv)\s+)?"
            r"файл(?:\s+с\s+названи\w*)?\s+([^,;]+?)(?:\s+(?:и\s+)?(?:запиши|напиши|впиши)\s+(.+))?$",
            normalized,
        )
        if create_named_folder:
            folder_res = self.tools.execute("desktop", {"action": "resolve", "query": create_named_folder.group(1).strip(), "hint": "folder"})
            if folder_res.success and isinstance(folder_res.data, dict):
                folder_path = str(folder_res.data.get("value", "") or "")
                arguments = {"action": "create_text_file", "folder": folder_path, "name": created_filename(create_named_folder.group(3), create_named_folder.group(2) or ""), "content": str(create_named_folder.group(4) or "")}
                execution = self.tools.execute("filesystem", arguments)
                self._remember_execution_context("filesystem", arguments, execution)
                return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        # Deterministic compound file creation. A small LLM should not have to
        # infer that "там" means the folder opened in the first clause. Resolve
        # the folder here and continue the hands-free filesystem workflow.
        create_text = re.match(
            r"^(?:открой|запусти)\s+(.+?)[,;]?\s+(?:(?:и|затем|потом)\s+)?"
            r"создай\s+(?:там\s+)?(?:(?:txt|текстов\w*)\s+)?файл(?:\s+с\s+названи\w*)?\s+"
            r"(.+?)\s+(?:(?:и|затем|потом)\s+)?(?:запиши|напиши|впиши)\s+"
            r"(?:в\s+(?:него|нем|файл)\s+)?(.+)$",
            normalized,
        )
        if create_text:
            folder_query = create_text.group(1).strip(" ,.-")
            file_name = create_text.group(2).strip(" ,.-\"'")
            content = create_text.group(3).strip()
            folder_res = self.tools.execute(
                "desktop", {"action": "resolve", "query": folder_query, "hint": "folder"}
            )
            if not folder_res.success or not isinstance(folder_res.data, dict):
                return AgentTurn(folder_res.text, tool_details=[f"desktop: {folder_res.text}"])
            folder_path = str(folder_res.data.get("value", "") or "")
            if not folder_path:
                return AgentTurn(f"Не удалось определить папку «{folder_query}».")
            # Opening the folder is safe and gives immediate visual feedback.
            self.tools.execute("filesystem", {"action": "open", "source": folder_path})
            if not file_name.casefold().endswith(".txt"):
                file_name += ".txt"
            arguments = {
                "action": "create_text_file",
                "folder": folder_path,
                "name": file_name,
                "content": content,
            }
            execution = self.tools.execute("filesystem", arguments)
            self._remember_execution_context("filesystem", arguments, execution)
            last_file_path = getattr(self, "_last_file_path", "")
            if execution.success and last_file_path:
                self.tools.execute("filesystem", {"action": "open", "source": last_file_path})
            return AgentTurn(execution.text, tool_details=[f"desktop: {folder_res.text}", f"filesystem: {execution.text}"])

        # Compound folder -> file workflow, including the exact conversational
        # form “открой папку Загрузки, открой там файл test.txt”.
        open_in_folder = re.match(
            r"^(?:открой|запусти)\s+(?:папк\w*\s+)?(.+?)[,;]?\s+"
            r"(?:(?:и|затем|потом)\s+)?(?:открой|запусти)\s+(?:там\s+)?(?:файл\w*\s+)?"
            r"(.+?\.[a-zа-я0-9]{1,10})$",
            normalized,
        )
        if open_in_folder:
            folder_query = open_in_folder.group(1).strip(" ,.-")
            file_query = open_in_folder.group(2).strip(' \\"«»')
            folder_res = self.tools.execute("desktop", {"action": "resolve", "query": folder_query, "hint": "folder"})
            if not folder_res.success or not isinstance(folder_res.data, dict):
                return AgentTurn(folder_res.text, tool_details=[f"desktop: {folder_res.text}"])
            folder_path = str(folder_res.data.get("value", "") or "")
            if not folder_path:
                return AgentTurn(f"Не удалось определить папку «{folder_query}».")
            folder_args = {"action": "open", "source": folder_path}
            opened_folder = self.tools.execute("filesystem", folder_args)
            self._remember_execution_context("filesystem", folder_args, opened_folder)
            candidate = self._resolve_context_file(file_query)
            if not candidate:
                return AgentTurn(f"Папка открыта: {folder_path}. Файл «{file_query}» в ней не найден.", tool_details=[f"filesystem: {opened_folder.text}"])
            file_args = {"action": "open", "source": candidate}
            opened_file = self.tools.execute("filesystem", file_args)
            self._remember_execution_context("filesystem", file_args, opened_file)
            return AgentTurn(
                f"Папка: {folder_path}. Файл открыт: {candidate}",
                tool_details=[f"filesystem: {opened_folder.text}", f"filesystem: {opened_file.text}"],
            )

        # Deterministic compound workflow: "открой Downloads и найди file report.pdf".
        open_find = re.match(
            r"^(?:открой|запусти)\s+(.+?)\s+(?:и|затем|потом)\s+(?:найди|отыщи)(?:\s+мне)?\s+(?:(файл\w*|file|папк\w*|folder)\s+)?(.+)$",
            normalized,
        )
        if open_find:
            folder_query = open_find.group(1).strip()
            item_kind = open_find.group(2) or "file"
            item_query = open_find.group(3).strip()
            folder_res = self.tools.execute("desktop", {"action": "resolve", "query": folder_query, "hint": "folder"})
            if not folder_res.success or not isinstance(folder_res.data, dict):
                return AgentTurn(folder_res.text, tool_details=[f"desktop: {folder_res.text}"])
            folder_path = str(folder_res.data.get("value", "") or "")
            self.tools.execute("filesystem", {"action": "open", "source": folder_path})
            find_res = self.tools.execute(
                "filesystem",
                {"action": "find", "source": folder_path, "query": item_query, "recursive": True, "limit": 20},
            )
            matches = find_res.data if isinstance(find_res.data, list) else []
            if matches:
                first = str(matches[0])
                self.tools.execute("filesystem", {"action": "reveal", "source": first})
                return AgentTurn(
                    f"Открыл {folder_path} и нашёл: {first}",
                    tool_details=[f"filesystem: {find_res.text}"],
                )
            # Fuzzy fallback inside normal user locations if spelling differs.
            fallback = self.tools.execute(
                "desktop",
                {"action": "resolve", "query": item_query, "hint": "folder" if "пап" in item_kind or item_kind == "folder" else "file"},
            )
            if fallback.success and isinstance(fallback.data, dict):
                found = str(fallback.data.get("value", "") or "")
                if found:
                    self.tools.execute("filesystem", {"action": "reveal", "source": found})
                    return AgentTurn(f"Открыл {folder_path} и нашёл: {found}")
            return AgentTurn(f"Открыл {folder_path}, но «{item_query}» внутри не нашёл.")

        find_match = re.match(
            r"^(?:найди|отыщи)(?:\s+мне)?\s+(файл\w*|папк\w*|file|folder)\s+(.+)$",
            normalized,
        )
        if find_match:
            target_type = find_match.group(1)
            query = find_match.group(2).strip()
            compound = any(marker in f" {query} " for marker in (",", ";", " и открой", " затем ", " потом "))
            if not compound:
                hint = "folder" if target_type.startswith("пап") or target_type == "folder" else "file"
                execution = self.tools.execute("desktop", {"action": "resolve", "query": query, "hint": hint})
                if execution.success:
                    data = execution.data if isinstance(execution.data, dict) else {}
                    value = str(data.get("value", "") or "")
                    reply = f"Нашёл: {value}" if value else execution.text
                    return AgentTurn(reply, tool_details=[f"desktop: {execution.text}"])
                return AgentTurn(execution.text, tool_details=[f"desktop: {execution.text}"])

        # Browser is also a semantic target type. "Запусти браузер" opens
        # the configured browser; "запусти браузер Chrome" explicitly opens that
        # browser application instead of searching for a folder named Chrome.
        explicit_browser = re.match(
            r"^(?:открой|запусти)(?:\s+мне)?\s+браузер(?:\s+(.+))?$",
            normalized, flags=re.I,
        )
        if explicit_browser:
            requested = str(explicit_browser.group(1) or "").strip(' "«».,;')
            browser_name = requested or self._preferred_browser()
            args = {"action": "open", "name": browser_name, "kind": "app"}
            execution = self.tools.execute("application", args)
            self._remember_execution_context("application", args, execution)
            self._remember_target_disambiguation(execution, kind="app")
            return AgentTurn(execution.text, tool_details=[f"application: {execution.text}"])

        # ---------- v2.3 typed desktop targets ----------
        # The noun after the action is a TYPE marker, never part of the searched
        # name: “открой игру Elden Ring” -> game="Elden Ring"; “открой файл
        # test.txt” -> file="test.txt". Apps/games use fuzzy executable discovery,
        # while explicit files/folders stay exact to avoid opening neighbours.
        explicit_app = re.match(
            r"^(?:открой|запусти|найди)(?:\s+мне)?\s+"
            r"(?:приложен\w*|программ\w*|app|application|program)\s+"
            r"(?:под\s+названи\w*\s+)?(.+)$",
            normalized,
        )
        if explicit_app:
            query = explicit_app.group(1).strip(' "«».,;')
            args = {"action": "open", "name": query, "kind": "app"}
            execution = self.tools.execute("application", args)
            self._remember_execution_context("application", args, execution)
            self._remember_target_disambiguation(execution, kind="app")
            return AgentTurn(execution.text, tool_details=[f"application: {execution.text}"])

        explicit_game = re.match(
            r"^(?:открой|запусти|найди)(?:\s+мне)?\s+"
            r"(?:игр\w*|game)\s+(?:под\s+названи\w*\s+)?(.+)$",
            normalized,
        )
        if explicit_game:
            query = explicit_game.group(1).strip(' "«».,;')
            args = {"action": "open", "name": query, "kind": "game"}
            execution = self.tools.execute("application", args)
            self._remember_execution_context("application", args, execution)
            self._remember_target_disambiguation(execution, kind="game")
            return AgentTurn(execution.text, tool_details=[f"application: {execution.text}"])

        explicit_file = re.match(
            r"^(?:открой|запусти|перейди\s+в)(?:\s+мне)?\s+"
            r"(?:файл\w*|file)\s+(?:под\s+названи\w*\s+)?(.+)$",
            normalized,
        )
        if explicit_file:
            query = self._normalize_spoken_filename(explicit_file.group(1).strip(' "«».,;'))
            # Exact current-folder path first; do not fuzzy-search sibling names.
            from pathlib import Path as _TypedPath
            last_folder = str(getattr(self, "_last_folder_path", "") or "")
            if last_folder:
                candidate = _TypedPath(last_folder) / query
                if candidate.is_file():
                    args = {"action": "open", "source": str(candidate)}
                    execution = self.tools.execute("filesystem", args)
                    self._remember_execution_context("filesystem", args, execution)
                    return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])
            args = {"action": "open", "query": query, "hint": "file"}
            execution = self.tools.execute("desktop", args)
            self._remember_execution_context("desktop", args, execution)
            return AgentTurn(execution.text, tool_details=[f"desktop: {execution.text}"])

        explicit_folder_typed = re.match(
            r"^(?:открой|запусти|перейди\s+в)(?:\s+мне)?\s+"
            r"(?:папк\w*|директор\w*|folder|directory)\s+"
            r"(?:под\s+названи\w*\s+)?(.+)$",
            normalized,
        )
        if explicit_folder_typed:
            query = explicit_folder_typed.group(1).strip(' "«».,;')
            args = {"action": "open", "query": query, "hint": "folder"}
            execution = self.tools.execute("desktop", args)
            self._remember_execution_context("desktop", args, execution)
            return AgentTurn(execution.text, tool_details=[f"desktop: {execution.text}"])

        # Continuous file workflow context is intentionally *file-shaped* only.
        # Earlier builds let any bare ``открой X`` consult the last folder first;
        # after editing a file this could make ``открой Steam`` open a similarly
        # named project/document instead of the Steam application.  Preserve the
        # convenient current-folder workflow for explicit ``файл ...`` commands
        # and obvious filenames, but let ordinary names reach the app-first desktop
        # resolver below.
        open_context_file = re.match(r"^(?:открой|запусти)\s+(?:(файл\w*)\s+)?(.+)$", normalized)
        if open_context_file and getattr(self, "_last_folder_path", "") and not re.search(r"(?:сайт|браузер|приложен|программ|игр\w*|\bgame\b|папк|директор)", normalized):
            explicit_file_word = bool(open_context_file.group(1))
            spoken_target = str(open_context_file.group(2) or "").strip()
            normalized_file = self._normalize_spoken_filename(spoken_target)
            suffix_match = re.search(r"\.([a-zа-яё0-9]{1,12})$", normalized_file, flags=re.I)
            file_like = bool(suffix_match and suffix_match.group(1).casefold() not in {"exe", "lnk", "url", "appref-ms"})
            if explicit_file_word or file_like:
                candidate = self._resolve_context_file(normalized_file)
                if candidate:
                    args = {"action": "open", "source": candidate}
                    execution = self.tools.execute("filesystem", args)
                    self._remember_execution_context("filesystem", args, execution)
                    return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        if re.match(r"^(?:открой|запусти)\s+(?:его|этот\s+файл|файл)$", normalized) and self._last_file_path:
            args = {"action": "open", "source": self._last_file_path}
            execution = self.tools.execute("filesystem", args)
            self._remember_execution_context("filesystem", args, execution)
            return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        move_current = re.match(r"^(?:перемести|перенеси)\s+(его|этот\s+файл|файл|эту\s+папку|папку)\s+(?:в|в папку)\s+(.+)$", normalized)
        if move_current:
            is_folder = "папк" in move_current.group(1)
            current = self._last_folder_path if is_folder else self._last_file_path
            if current:
                dest_res = self.tools.execute("desktop", {"action": "resolve", "query": move_current.group(2).strip(), "hint": "folder"})
                if dest_res.success and isinstance(dest_res.data, dict):
                    from pathlib import Path
                    folder = str(dest_res.data.get("value", "") or "")
                    target = str(Path(folder) / Path(current).name)
                    args = {"action": "move", "source": current, "destination": target}
                    execution = self.tools.execute("filesystem", args)
                    self._remember_execution_context("filesystem", args, execution)
                    return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        delete_current = re.match(r"^(?:удали|убери)\s+(его|этот\s+файл|файл|эту\s+папку|папку)(?:\s+в\s+корзин\w*)?$", normalized)
        if delete_current:
            is_folder = "папк" in delete_current.group(1)
            current = self._last_folder_path if is_folder else self._last_file_path
            if current:
                args = {"action": "delete", "source": current}
                execution = self.tools.execute("filesystem", args)
                self._remember_execution_context("filesystem", args, execution)
                return AgentTurn(execution.text, tool_details=[f"filesystem: {execution.text}"])

        close_current_file = re.match(r"^закрой\s+(?:его|этот\s+файл|файл|документ)$", normalized)
        if close_current_file and self._last_file_path:
            # Prefer the actual document window. This works for Notepad and most
            # editors even when the process itself is named notepad.exe/editor.exe.
            from pathlib import Path as _ClosePath
            file_name = _ClosePath(self._last_file_path).name
            execution = self.tools.execute("window", {"action": "close", "title": file_name})
            if not execution.success:
                execution = self.tools.execute("input_control", {"action": "hotkey", "keys": ["alt", "f4"]})
            return AgentTurn(f"Закрыл файл: {self._last_file_path}", tool_details=[f"window: {execution.text}"])

        process_info = re.match(r"^(?:найди|покажи|дай)\s+(?:мне\s+)?(?:информац\w*\s+(?:о|про)\s+)?процесс\w*\s+(.+)$", normalized)
        if process_info:
            args = {"action": "info", "name": process_info.group(1).strip()}
            execution = self.tools.execute("application", args)
            return AgentTurn(execution.text, tool_details=[f"application: {execution.text}"])

        force_kill = re.match(r"^(?:убей|принудительно\s+(?:закрой|заверши)|kill)\s+(?:процесс\w*\s+)?(.+)$", normalized)
        if force_kill:
            args = {"action": "kill", "name": force_kill.group(1).strip()}
            execution = self.tools.execute("application", args)
            return AgentTurn(execution.text, tool_details=[f"application: {execution.text}"])

        close_match = re.match(r"^(?:закрой|заверши|останови)\s+(.+)$", normalized)
        if close_match and not media_object:
            target_name = close_match.group(1).strip()
            target_name = re.sub(r"^(?:процесс|приложение|программу|программа|окно|файл|документ)\s+", "", target_name).strip()
            # A visible document/window must be closed as a window first. `test.txt`
            # is never a process name, and older builds therefore reported
            # “Процессы не найдены: test.txt”.
            looks_like_file = bool(re.search(r"\.[a-zа-яё0-9]{1,8}$", target_name, re.IGNORECASE))
            if looks_like_file:
                execution = self.tools.execute("window", {"action": "close", "title": target_name})
                if execution.success:
                    return AgentTurn(execution.text, tool_details=[f"window: {execution.text}"])
                if self._last_file_path:
                    from pathlib import Path as _ClosePath
                    if _ClosePath(self._last_file_path).name.casefold() == target_name.casefold():
                        execution = self.tools.execute("input_control", {"action": "hotkey", "keys": ["alt", "f4"]})
                        return AgentTurn(f"Закрыл файл: {self._last_file_path}", tool_details=[f"input_control: {execution.text}"])
            # For ordinary app names first close the matching visible window; if
            # there is no window (tray/background app), terminate matching process.
            window_execution = self.tools.execute("window", {"action": "close", "title": target_name})
            if window_execution.success:
                return AgentTurn(window_execution.text, tool_details=[f"window: {window_execution.text}"])
            execution = self.tools.execute("application", {"action": "close", "name": target_name})
            return AgentTurn(execution.text, tool_details=[f"application: {execution.text}"])

        # Canonical Windows shell folders have human names rather than app names.
        # Route them directly through the folder-locked desktop resolver before
        # the generic «открой X» application-compatible branch.  This keeps
        # Рабочий стол/Загрузки/Документы/Корзина/This PC in File Explorer's
        # ownership domain even when the user does not say the word «папка».
        known_shell_open = re.fullmatch(
            r"(?:открой|запусти|покажи|перейди\s+(?:в|на))\s+"
            r"(рабочий\s+стол|desktop|загрузк(?:и|у)|downloads?|документ(?:ы|ам)|documents?|"
            r"картинк(?:и|ам)|изображени(?:я|ям)|pictures?|домашн(?:яя|ю)\s+папк(?:а|у)|home|"
            r"профил(?:ь|я)|корзин(?:а|у)|recycle\s*bin|этот\s+компьютер|мой\s+компьютер|this\s+pc)",
            normalized, flags=re.I,
        )
        if known_shell_open:
            folder_query = known_shell_open.group(1).strip(' "«».,;')
            desktop_args = {"action": "open", "query": folder_query, "hint": "folder"}
            execution = self.tools.execute("desktop", desktop_args)
            self._remember_execution_context("desktop", desktop_args, execution)
            return AgentTurn(execution.text, tool_details=[f"desktop: {execution.text}"])

        # Explicit folder commands always preserve the literal user name.
        # Never ask the LLM to translate Important -> «Важные документы».
        explicit_folder = re.match(r"^(?:открой|запусти)\s+папк\w*\s+(.+)$", normalized)
        if explicit_folder:
            folder_query = explicit_folder.group(1).strip(' \"«».,;')
            if folder_query and not any(marker in f" {folder_query} " for marker in (",", ";", " и создай", " и открой", " и найди", " затем ", " потом ")):
                desktop_args = {"action": "open", "query": folder_query, "hint": "folder"}
                execution = self.tools.execute("desktop", desktop_args)
                self._remember_execution_context("desktop", desktop_args, execution)
                return AgentTurn(execution.text, tool_details=[f"desktop: {execution.text}"])

        open_match = re.match(r"^(?:открой|запусти)\s+(.+)$", normalized)
        if open_match:
            target_name = open_match.group(1).strip()
            compound_markers = (
                ",", ";", " затем ", " потом ", " и создай", " и запиши", " и найди",
                " и напиши", " и перемести", " и переимен", " после этого ",
            )
            if any(marker in f" {target_name} " for marker in compound_markers):
                return None

            settings_pages = {
                "настройки звука": "sound", "параметры звука": "sound",
                "настройки экрана": "display", "параметры экрана": "display",
                "настройки сети": "network", "параметры сети": "network",
                "настройки блютуз": "bluetooth", "параметры блютуз": "bluetooth",
            }
            if target_name in settings_pages:
                execution = self.tools.execute("system", {"action": "open_settings", "page": settings_pages[target_name]})
                return AgentTurn(execution.text, tool_details=[f"system: {execution.text}"])

            # Browser use is explicit. “Открой Яндекс Музыку” remains an app
            # request; “открой в браузере/интернете Яндекс Музыку” is web.
            web_prefix = re.match(
                r"^(?:в\s+(?:браузер\w*|интернете|сети)|через\s+браузер\w*)\s+(.+)$",
                target_name, flags=re.I,
            )
            if web_prefix:
                target_name = web_prefix.group(1).strip()
                hint = "web"
            elif re.match(r"^(?:сайт\w*|страниц\w*|website|site|page)\s+", target_name):
                hint = "web"
            elif re.match(r"^(?:папк\w*|директор\w*|folder|directory)\s+", target_name):
                hint = "folder"
            elif re.match(r"^(?:файл\w*|file)\s+", target_name):
                hint = "file"
            elif re.match(r"^(?:приложен\w*|программ\w*|app|application|program)\s+", target_name):
                hint = "app"
            elif re.match(r"^(?:игр\w*|game)\s+", target_name):
                hint = "game"
            else:
                hint = "auto"
            desktop_args = {"action": "open", "query": target_name, "hint": hint}
            execution = self.tools.execute("desktop", desktop_args)
            self._remember_execution_context("desktop", desktop_args, execution)
            data = execution.data if isinstance(getattr(execution, "data", None), dict) else {}
            if bool(data.get("requires_clarification")):
                self._remember_target_disambiguation(execution, kind="game" if hint == "game" else "app")
            return AgentTurn(execution.text, tool_details=[f"desktop: {execution.text}"])

        return None

