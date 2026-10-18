# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import logging
import hashlib
from jarvis.runtime import cached_re as re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable
from urllib.parse import urlparse

from jarvis.gaming.manager import GameManager, game_presence_service
from jarvis.gaming.normalize import norm
from jarvis.gaming.routing_firewall import (
    is_explicit_game_session_query,
    is_explicit_general_non_game_query,
    is_explicit_player_state_query,
    is_information_seeking_turn,
)
from jarvis.gaming.runtime import active_game_context, process_identity_matches
from jarvis.runtime.semantics import looks_like_browser_search, looks_like_general_question, looks_like_web_lookup, strip_browser_search_scaffolding
from jarvis.routing.ownership_firewall import current_turn_claim, classify_turn_ownership

from .cache import GameWebCache
from .context import GameWebContextStore
from .searchapi import SearchAPIGoogleProvider

logger = logging.getLogger(__name__)

_GAME_CUE_RE = re.compile(
    r"\b(?:игр\w*|game\w*|оруж\w*|weapon\w*|класс\w*|class\w*|персонаж\w*|character\w*|"
    r"талант\w*|talent\w*|способност\w*|skill\w*|билд\w*|build\w*|перк\w*|perk\w*|"
    r"босс\w*|boss\w*|мисси\w*|mission\w*|квест\w*|quest\w*|предмет\w*|item\w*|"
    r"ресурс\w*|resource\w*|брон\w*|armor\w*|урон\w*|damage\w*|дроп\w*|drop\w*|"
    r"крафт\w*|craft\w*|лут\w*|loot\w*|карт\w*|map\w*|враг\w*|enemy\w*|"
    r"моб\w*|mob\w*|рейд\w*|raid\w*|подземел\w*|dungeon\w*|сезон\w*|season\w*|"
    r"патч\w*|patch\w*|мета\w*|гайд\w*|guide\w*|прокач\w*|уров(?:ень|н\w*)|лвл\w*|level\w*|leveling\w*|"
    r"spiel\w*|jeu\w*|juego\w*|gioc[oh]\w*|jogo\w*|niveau\w*|nivel\w*|livell\w*|"
    r"фарм\w*|farm\w*|выб\w*|добы\w*|unlock\w*|компонент\w*|component\w*|"
    r"чертеж\w*|blueprint\w*|рецепт\w*|recipe\w*|падает\w*|выпада\w*|drop\w*)\b", re.I,
)
_ACQUIRE_RE = re.compile(
    r"\b(?:где\s+(?:взять|найти|получить|добыть|фармить|выбить)|как\s+(?:получить|найти|добыть|скрафтить|выбить|зафармить|фармить|достать|открыть|разблокировать)|"
    r"что\s+делает|для\s+чего|кем\s+играть|кого\s+выбрать|какой\s+билд|какое\s+оружие|"
    r"where\s+to\s+(?:get|find|farm)|how\s+to\s+(?:get|find|craft|farm)|what\s+does|best\s+build|best\s+weapon)\b", re.I,
)
_SHORT_FOLLOWUP_RE = re.compile(r"^(?:а\s+)?(?:где|как|почему|зачем|какой|какая|какое|какие|сколько|что|кто|чем|и\s+что)\b", re.I)
_YES_RE = re.compile(r"^(?:да|ага|угу|давай|конечно|хорошо|ок(?:ей)?|yes|yeah|yep|sure|go ahead)[.! ]*$", re.I)
_NO_RE = re.compile(r"^(?:нет|неа|не надо|отмена|no|nope)[.! ]*$", re.I)
from jarvis.runtime.confirm_words187 import guard_no as _guard_no187, guard_yes as _guard_yes187
_YES_RE, _NO_RE = _guard_yes187(_YES_RE), _guard_no187(_NO_RE)  # NEO 187 HREQ-014
_EXPLICIT_UNKNOWN_RE = re.compile(r"\b(?:в\s+игре|из\s+игры|для\s+игры|in\s+(?:the\s+)?game)\s+([A-Za-zА-Яа-я0-9][^?!,.;]{1,80})", re.I)


@dataclass(slots=True)
class _Conversation:
    game_id: str = ""
    game_name: str = ""
    question: str = ""
    answer: str = ""
    search_query: str = ""
    provider: str = ""
    continuation_token: str = ""
    related_questions: tuple[str, ...] = ()
    pending_followup: str = ""
    updated_at: float = 0.0


@dataclass(slots=True)
class _ForcedFollowup:
    game_id: str = ""
    game_name: str = ""
    question: str = ""
    continuation_token: str = ""
    previous_question: str = ""
    previous_answer: str = ""
    created_at: float = 0.0


class GameWebKnowledgeService:
    """SearchAPI Google fact retrieval with exact provenance and no LLM fallback."""

    def __init__(self, config: Any, *, ollama: Any, active_game_provider: Callable[[], str] | None = None) -> None:
        self.config = config
        self.ollama = ollama
        self.active_game_provider = active_game_provider
        self.manager = GameManager(enabled=bool(config.get("gaming.enabled", True)), config=config)
        self.cache = GameWebCache(config)
        self.searcher = SearchAPIGoogleProvider(config)
        self.context_store = GameWebContextStore(config)
        self._conversation = _Conversation()
        self._forced = _ForcedFollowup()
        self._missing_game: tuple[str, float] = ("", 0.0)
        self._conversation_scope = ("desktop", str(config.get("app.language", "ru")))
        self._lock = threading.RLock()

    def close(self) -> None:
        self.searcher.close()

    def adopt_dialogue(self, previous: "GameWebKnowledgeService | None") -> bool:
        """HIST-009: a Settings Apply keeps the open game conversation.

        Only the dialogue (pending title question, last answer, offered follow-up)
        moves to the fresh service; provider caches stay fresh on purpose. A changed
        language still resets in begin_turn(), and disabled gaming starts empty.
        """
        if previous is None or previous is self or not bool(self.config.get("gaming.enabled", True)):
            return False
        lock = getattr(previous, "_lock", None)
        if lock is None:
            return False
        with lock:
            moved = (previous._conversation, previous._forced, previous._missing_game, previous._conversation_scope)
        with self._lock:
            self._conversation, self._forced, self._missing_game, self._conversation_scope = moved
        return True

    def reset_context(self) -> None:
        with self._lock:
            self._conversation = _Conversation()
            self._forced = _ForcedFollowup()
            self._missing_game = ("", 0.0)

    def abandon_title(self) -> None:
        with self._lock:
            self._missing_game = ("", 0.0)

    def begin_turn(self, scope: str, language: str, text: str) -> bool:
        """Return True only when a bare cancellation consumes a pending title."""
        with self._lock:
            if (scope, language) != self._conversation_scope:
                self.reset_context()
                self._conversation_scope = (scope, language)
            question, expires = self._missing_game
            if not question or time.monotonic() >= expires:
                self._missing_game = ("", 0.0)
                return False
            if norm(text) in {"отмена", "не надо", "стоп", "cancel", "stop", "abbrechen", "annuler", "cancelar", "annulla"}:
                self._missing_game = ("", 0.0)
                return True
            claim = classify_turn_ownership(text)
            from jarvis.runtime.addressing import fast_social_reply
            from jarvis.runtime.natural_commands import looks_like_new_command, norm as dialogue_norm
            from jarvis.runtime.social_atlas import catalog
            # These can return early in Agent before the normal web lane runs.
            # They must not leave the old title question armed behind that reply.
            if ((claim.enforced and claim.owner != "game_information")
                    or is_information_seeking_turn(text)
                    or looks_like_new_command(text, language)
                    or fast_social_reply(text, language) is not None
                    or dialogue_norm(text) in catalog(language)):
                self._missing_game = ("", 0.0)
            return False

    def _text(self, rows: tuple[str, ...]) -> str:
        from jarvis.language import language_code
        codes = ("ru", "en", "de", "fr", "es", "it", "pt")
        code = language_code(self.config)
        return rows[codes.index(code) if code in codes else 1]

    def unavailable_message(self, error: str, *, general: bool = False) -> str:
        from jarvis.runtime.dialogue_messages import message
        from jarvis.language import language_code
        if general and error in {"searchapi_answer_missing", "searchapi_no_results"}:
            # C-9: the honest limit of a general question (no game title to clarify).
            return self._text(("SearchAPI.io не вернул полезных результатов по этому вопросу. Если хочешь, отвечу без поиска — скажи «ответь без поиска».",
                "SearchAPI.io returned no useful results for this question. If you like, I can answer without a search — say “answer without searching”.",
                "SearchAPI.io hat zu dieser Frage nichts Brauchbares geliefert. Ich kann auch ohne Suche antworten — sag „antworte ohne Suche“.",
                "SearchAPI.io n’a rien renvoyé d’utile pour cette question. Je peux répondre sans recherche — dis « réponds sans recherche ».",
                "SearchAPI.io no devolvió resultados útiles para esta pregunta. Puedo responder sin buscar: di «responde sin buscar».",
                "SearchAPI.io non ha restituito risultati utili per questa domanda. Posso rispondere senza cercare: di’ «rispondi senza cercare».",
                "O SearchAPI.io não devolveu resultados úteis para esta pergunta. Posso responder sem pesquisar — diz «responde sem pesquisar»."))
        if general and error == "searchapi_disabled":
            return self._text(("Интернет-поиск выключен (SearchAPI.io в Настройки → API-сервисы). Без поиска ответить могу, если скажешь «ответь без поиска».",
                "Web search is disabled (SearchAPI.io in Settings → API services). I can answer without it if you say “answer without searching”.",
                "Die Websuche ist ausgeschaltet (SearchAPI.io unter Einstellungen → API-Dienste). Ohne Suche antworte ich, wenn du „antworte ohne Suche“ sagst.",
                "La recherche web est désactivée (SearchAPI.io dans Paramètres → Services API). Je réponds sans elle si tu dis « réponds sans recherche ».",
                "La búsqueda web está desactivada (SearchAPI.io en Ajustes → Servicios API). Respondo sin ella si dices «responde sin buscar».",
                "La ricerca web è disattivata (SearchAPI.io in Impostazioni → Servizi API). Rispondo senza se dici «rispondi senza cercare».",
                "A pesquisa web está desativada (SearchAPI.io em Definições → Serviços API). Respondo sem ela se disseres «responde sem pesquisar»."))
        common = {"searchapi_key_missing": "web_key", "searchapi_auth_error": "web_auth",
                  "searchapi_rate_limited": "web_limit", "searchapi_timeout": "web_timeout"}
        if error in common:
            return message(common[error], language_code(self.config))
        if error == "searchapi_disabled":
            return self._text(("Интернет-поиск выключен. Включи SearchAPI.io в Настройки → API-сервисы.",
                "Web search is disabled. Enable SearchAPI.io in Settings → API services.",
                "Die Websuche ist ausgeschaltet. Aktiviere SearchAPI.io unter Einstellungen → API-Dienste.",
                "La recherche web est désactivée. Active SearchAPI.io dans Paramètres → Services API.",
                "La búsqueda web está desactivada. Activa SearchAPI.io en Ajustes → Servicios API.",
                "La ricerca web è disattivata. Attiva SearchAPI.io in Impostazioni → Servizi API.",
                "A pesquisa web está desativada. Ativa SearchAPI.io em Definições → Serviços API."))
        if error == "searchapi_billing_required":
            return self._text(("SearchAPI.io сообщает, что закончились доступные запросы. Проверь тариф и остаток в аккаунте.",
                "SearchAPI.io reports no available credits. Check your plan and balance.",
                "SearchAPI.io meldet fehlendes Guthaben. Prüfe deinen Tarif und Kontostand.",
                "SearchAPI.io signale un crédit insuffisant. Vérifie ton forfait et ton solde.",
                "SearchAPI.io indica que no hay créditos disponibles. Comprueba tu plan y saldo.",
                "SearchAPI.io segnala crediti insufficienti. Controlla il piano e il saldo.",
                "O SearchAPI.io indica que não há créditos disponíveis. Verifica o plano e o saldo."))
        if error.startswith("searchapi_network_error:") or error == "searchapi_server_error":
            return self._text(("Обнаружена высокая задержка или сбой сети. Не удалось получить ответ SearchAPI.io. Повтори вопрос позже.",
                "The network or search service is unavailable. Try your question again later.",
                "Das Netzwerk oder der Suchdienst ist nicht erreichbar. Versuche die Frage später erneut.",
                "Le réseau ou le service de recherche est indisponible. Réessaie plus tard.",
                "La red o el servicio de búsqueda no está disponible. Repite la pregunta más tarde.",
                "La rete o il servizio di ricerca non è disponibile. Riprova più tardi.",
                "A rede ou o serviço de pesquisa está indisponível. Repete a pergunta mais tarde."))
        if error in {"searchapi_answer_missing", "searchapi_no_results"}:
            return self._text(("SearchAPI.io не нашёл данных. Уточни вопрос или название игры.",
                "SearchAPI.io found no results. Clarify the question or game title.",
                "SearchAPI.io hat keine Ergebnisse gefunden. Präzisiere die Frage oder den Spieltitel.",
                "SearchAPI.io n’a trouvé aucun résultat. Précise la question ou le titre du jeu.",
                "SearchAPI.io no encontró resultados. Aclara la pregunta o el título del juego.",
                "SearchAPI.io non ha trovato risultati. Precisa la domanda o il titolo del gioco.",
                "O SearchAPI.io não encontrou resultados. Esclarece a pergunta ou o título do jogo."))
        return message("web_no_result", language_code(self.config))

    def pending_game_title(self, language: str, *, scope: str = "desktop", now: float | None = None) -> tuple[str, float] | None:
        with self._lock:
            question, expires = self._missing_game
            at = time.monotonic() if now is None else now
            if (scope, language) != self._conversation_scope or not question or at >= expires:
                return None
            return question, expires

    def _resume_game_title(self, text: str) -> str:
        with self._lock:
            question, expires = self._missing_game
            if not question or time.monotonic() > expires:
                self._missing_game = ("", 0.0)
                return ""
        raw = str(text or "").strip()
        claim = classify_turn_ownership(raw)
        # Do not turn a new command, social reply, or new question into a title.
        if (claim.enforced and claim.owner != "game_information") or is_information_seeking_turn(raw):
            with self._lock:
                self._missing_game = ("", 0.0)
            return ""
        from jarvis.runtime.natural_commands import looks_like_new_command
        from jarvis.language import language_code
        if looks_like_new_command(raw, language_code(self.config)) or norm(raw) in {
            "да", "нет", "привет", "спасибо", "дальше", "назад", "продолжить", "стоп", "отмена",
            "yes", "no", "hello", "hi", "thanks", "next", "back", "continue", "stop", "cancel",
            "hallo", "danke", "bonjour", "merci", "hola", "gracias", "ciao", "grazie", "ola", "obrigado"}:
            with self._lock:
                self._missing_game = ("", 0.0)
            return ""
        if not raw or len(raw) > 100 or len(raw.split()) > 10 or re.search(r"[?!;\n]", raw):
            return ""
        game = self._explicit_game(raw)
        if not game:
            title = raw.strip(' «»"')
            game = {"game_id": self._slug(title), "game_name": title}
        with self._lock:
            self._forced = _ForcedFollowup(str(game["game_id"]), self._display_game_name(game),
                question, "", "", "", time.time())
            self._missing_game = ("", 0.0)
        return question

    def _runtime_game(self) -> dict[str, Any]:
        current = active_game_context(max_age_seconds=180.0) or {}
        if current:
            # Shared runtime validates foreground plus PID/create-time identity.
            return dict(current)
        # Agent supplies the same process-verified game snapshot used by routing.
        # Prefer that cheap shared snapshot to another process scan.
        if callable(self.active_game_provider):
            try:
                game_id = str(self.active_game_provider() or "").casefold()
                adapter = next((row for row in self.manager.adapters if row.game_id.casefold() == game_id), None)
                if adapter is not None:
                    return {"game_id": adapter.game_id, "game_name": adapter.display_name}
            except Exception:
                logger.debug("Active game callback unavailable", exc_info=True)
        persisted = self.context_store.read()
        # A saved game is a preference, never proof of the foreground game.
        # Reject PID reuse before even offering it to the foreground-aware service.
        if str(persisted.get("status") or "") != "running" or not process_identity_matches(persisted):
            persisted = {}
        try:
            snap = game_presence_service(self.manager).snapshot(
                enabled=bool(self.config.get("gaming.enabled", True)), max_age_seconds=1.0, force=False,
                preferred_pid=int(persisted.get("pid", 0) or 0) or None,
                preferred_game_id=str(persisted.get("game_id") or ""),
            )
            return snap.as_dict() if snap is not None else {}
        except Exception:
            return {}

    @staticmethod
    def _slug(value: str) -> str:
        normalized = norm(value)
        out = re.sub(r"[^a-z0-9]+", "_", normalized, flags=re.I).strip("_")
        # Unsupported Cyrillic/non-Latin titles must never share `other_game`
        # and reuse another game's answer to the same short question.
        if not out or any(ord(char) > 127 for char in normalized):
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
            return (out[:60] or "game") + "_" + digest
        return out[:80]

    def _explicit_game(self, text: str) -> dict[str, Any]:
        t = norm(text)
        if not t:
            return {}
        best: tuple[float, Any] | None = None
        words = t.split()
        for adapter in self.manager.adapters:
            aliases = [adapter.display_name, adapter.game_id.replace("_", " "), *adapter.aliases]
            score = 0.0
            for alias in aliases:
                a = norm(alias)
                if not a:
                    continue
                if re.search(rf"(?<!\w){re.escape(a)}(?!\w)", t):
                    score = max(score, 1.0)
                    continue
                aw = a.split()
                if len(aw) == 1:
                    for w in words:
                        if len(w) >= 4 and len(a) >= 4:
                            matcher = SequenceMatcher(None, w, a)
                            lower = max(0.80, score)
                            if matcher.real_quick_ratio() >= lower and matcher.quick_ratio() >= lower:
                                score = max(score, matcher.ratio())
                else:
                    n = len(aw)
                    for width in {max(1, n - 1), n, n + 1}:
                        for i in range(max(0, len(words) - width + 1)):
                            chunk = " ".join(words[i:i + width])
                            matcher = SequenceMatcher(None, chunk, a)
                            lower = max(0.80, score)
                            if matcher.real_quick_ratio() >= lower and matcher.quick_ratio() >= lower:
                                score = max(score, matcher.ratio())
            if best is None or score > best[0]:
                best = (score, adapter)
        if best and best[0] >= 0.80:
            adapter = best[1]
            return {"game_id": adapter.game_id, "game_name": adapter.display_name, "explicit_confidence": best[0]}

        # Explicitly naming an unsupported title should still prevent pollution by
        # the currently running game.  The exact user query is sent to Google AI Mode.
        m = _EXPLICIT_UNKNOWN_RE.search(str(text or ""))
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            # Trim obvious trailing request words only if capture became broad.
            name = re.split(r"\s+(?:как|где|что|какой|какая|какое|какие)\b", name, maxsplit=1, flags=re.I)[0].strip()
            if name:
                return {"game_id": self._slug(name), "game_name": name, "explicit_unknown": True}
        return {}

    def _recent_conversation(self) -> _Conversation | None:
        with self._lock:
            c = self._conversation
            if not c.game_id:
                return None
            ttl = float(self.config.get("gaming.web_knowledge.followup_context_seconds", 600) or 600)
            if time.time() - c.updated_at > max(30.0, min(3600.0, ttl)):
                return None
            return _Conversation(c.game_id, c.game_name, c.question, c.answer, c.search_query, c.provider,
                                 c.continuation_token, tuple(c.related_questions), c.pending_followup, c.updated_at)

    def pending_followup(self) -> str:
        c = self._recent_conversation()
        return c.pending_followup if c else ""

    def resume_pending_followup(self, text: str) -> str:
        title_answer = self._resume_game_title(text)
        if title_answer:
            return title_answer
        c = self._recent_conversation()
        if c is None or not c.pending_followup:
            return ""
        raw = str(text or "").strip()
        if _NO_RE.fullmatch(raw):
            with self._lock:
                self._conversation.pending_followup = ""
            return ""
        if not _YES_RE.fullmatch(raw):
            return ""
        with self._lock:
            self._forced = _ForcedFollowup(
                c.game_id, c.game_name, c.pending_followup, c.continuation_token,
                c.question, c.answer, time.time(),
            )
            self._conversation.pending_followup = ""
        logger.info("GameWeb dialogue resume game=%s pending=%r user=%r", c.game_id, c.pending_followup[:180], raw)
        return c.pending_followup

    def _forced_for(self, text: str) -> _ForcedFollowup | None:
        with self._lock:
            f = self._forced
            if not f.game_id or time.time() - f.created_at > 30.0:
                return None
            if norm(text) != norm(f.question):
                return None
            return _ForcedFollowup(f.game_id, f.game_name, f.question, f.continuation_token,
                                   f.previous_question, f.previous_answer, f.created_at)

    def _is_followup(self, text: str) -> bool:
        t = norm(text); words = t.split()
        if not t or len(words) > 12:
            return False
        if re.search(r"\b(?:это|этого|этом|этот|эта|эти|он|она|они|его|ее|её|их|it|this|that|them)\b", t):
            return True
        if len(words) <= 7 and re.match(r"^(?:а|и)\s+", t):
            return True
        if len(words) <= 5 and _SHORT_FOLLOWUP_RE.search(t) and not (_GAME_CUE_RE.search(t) or _ACQUIRE_RE.search(t)):
            return True
        return False

    def _scope(self, text: str) -> tuple[dict[str, Any], str]:
        forced = self._forced_for(text)
        if forced is not None:
            return {"game_id": forced.game_id, "game_name": forced.game_name}, "dialogue_accept"
        explicit = self._explicit_game(text)
        if explicit:
            return explicit, "explicit_game"
        recent = self._recent_conversation()
        if recent is not None and self._is_followup(text):
            return {"game_id": recent.game_id, "game_name": recent.game_name}, "web_followup"
        current = self._runtime_game()
        if current and (_GAME_CUE_RE.search(norm(text)) or _ACQUIRE_RE.search(norm(text))):
            return current, "active_game_cue"
        return {}, ""

    @staticmethod
    def _local_domain_control(text: str) -> bool:
        """Hard firewall: an active game never owns local control namespaces.

        These are command shells, not topic words. In particular this prevents
        "покажи очередь треков" from becoming a Tarkov/Warframe web query just
        because a game is currently running.
        """
        t = norm(text)
        if not t:
            return False
        if re.search(r"\b(?:очеред\w*|трек\w*|песн\w*|музык\w*|моя\s+волна|мою\s+волну|мои\s+треки|"
                     r"discord|дискорд\w*|яндекс\w*\s+музык\w*|yandex\s+music|youtube|ютуб\w*)\b", t, re.I):
            if re.search(r"\b(?:покажи|назови|перечисли|очисти|сбрось|удали|включи|выключи|запусти|поставь|"
                         r"пауза|продолжи|дальше|следующ\w*|предыдущ\w*|лайк|дизлайк|перемеш\w*|shuffle|что\s+сейчас\s+играет)\b", t, re.I):
                return True
        if re.search(r"^(?:покажи|выведи|открой|закрой|перемести|перенеси|установи|поставь|включи|выключи|"
                     r"сделай|измени|очисти|запусти|останови)\b", t, re.I) and re.search(
            r"\b(?:систем\w*|процессор\w*|cpu|ram|памят\w*|окн\w*|монитор\w*|экран\w*|громк\w*|"
            r"микрофон\w*|bluetooth|блютуз\w*|панел\w*|заметк\w*|чек.?лист\w*)\b", t, re.I
        ):
            return True
        return False

    def owns(self, text: str) -> tuple[bool, dict[str, Any]]:
        claim = current_turn_claim()
        # HOTFIX92: active-game context is subordinate to the turn owner. Local
        # browser/file/system/media commands can never be stolen by GameWeb.
        if claim.enforced and claim.owner != "game_information":
            return False, {}
        # "найди в браузере ..." is the one explicit opt-in to opening a search
        # page. Even a named/active game must not steal that browser command.
        if looks_like_browser_search(text):
            return False, {}
        if self._local_domain_control(text):
            return False, {}
        if is_explicit_game_session_query(text) or is_explicit_player_state_query(text) or is_explicit_general_non_game_query(text):
            return False, {}
        forced = self._forced_for(text)
        # Explicit game name + unmistakable gameplay vocabulary is enough even
        # when ASR corrupts the question word (field example: "так выбить ... в
        # Warframe" instead of "как выбить ..."). This still does not steal
        # launch/control commands such as "запусти Warframe" because they lack
        # a gameplay-information cue.
        explicit_game = self._explicit_game(text)
        explicit_game_cue = bool(explicit_game and _GAME_CUE_RE.search(norm(text)))
        multilingual_question = bool(re.search(r"\b(?:wie|was|wo|welche|combien|comment|quel|où|cuántos|cuantas|cómo|dónde|quanti|quante|come|dove|quantos|quantas|como|onde)\b", norm(text), re.I))
        if forced is None and not is_information_seeking_turn(text) and not explicit_game_cue and not multilingual_question:
            return False, {}
        game, reason = self._scope(text)
        if not game and claim.owner == "game_information":
            # The owner is stronger than fuzzy title parsing. Try the runtime game
            # once more; if the utterance explicitly says "в игре <name>", keep
            # even an unsupported/ASR-damaged title in the game web namespace.
            game = self._explicit_game(text) or self._runtime_game()
            reason = "ownership_forced_game" if game else ""
        if not game:
            if claim.owner == "game_information" or re.search(r"\b(?:игр\w*|game\w*|spiel\w*|jeu\w*|juego\w*|gioc[oh]\w*|jogo\w*)\b", norm(text), re.I):
                return True, {"scope_reason": "missing_game", "needs_game_title": True}
            return False, {}
        return True, {**game, "scope_reason": reason}

    @staticmethod
    def _display_game_name(game: dict[str, Any]) -> str:
        return str(game.get("game_name") or game.get("name") or game.get("game_id") or "game").strip()

    def _cache_question(self, text: str, game: dict[str, Any]) -> str:
        forced = self._forced_for(text)
        if forced is not None:
            return f"{forced.previous_question} -> {text}".strip() if forced.previous_question else str(text or "").strip()
        recent = self._recent_conversation()
        if recent is not None and recent.game_id == str(game.get("game_id") or "") and self._is_followup(text):
            return f"{recent.question} -> {str(text or '').strip()}"
        return str(text or "").strip()

    def _build_search_query(self, text: str, game: dict[str, Any], scope_reason: str) -> tuple[str, str]:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if scope_reason == "explicit_game":
            return raw[:1800], ""
        game_name = self._display_game_name(game)
        forced = self._forced_for(text)
        recent = self._recent_conversation()
        continuation = ""
        previous_q = previous_a = ""
        if forced is not None:
            continuation = forced.continuation_token
            previous_q, previous_a = forced.previous_question, forced.previous_answer
        elif recent is not None and recent.game_id == str(game.get("game_id") or "") and self._is_followup(text):
            continuation = recent.continuation_token
            previous_q, previous_a = recent.question, recent.answer
        if continuation:
            # Retained for compatibility with older dialogue state. Google AI Mode
            # has no server-side continuation token, so context is rebuilt below.
            continuation = ""
        if previous_q and previous_a:
            context = re.sub(r"\s+", " ", previous_a).strip()[:900]
            query = f"{game_name}. Предыдущий вопрос: {previous_q}. Предыдущий ответ: {context}. Новый вопрос: {raw}"
            return query[:1800], ""
        if norm(game_name) not in norm(raw):
            raw = f"{game_name} {raw}"
        return raw[:1800], ""

    @staticmethod
    def _domain(url: str) -> str:
        try: return (urlparse(url).hostname or "").removeprefix("www.")
        except Exception: return ""

    @staticmethod
    def _clean_provider_answer(value: str) -> str:
        # Preserve Google AI answer structure/markdown. Only trim horizontal-space
        # noise; never paraphrase or run the answer through the local model.
        lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in str(value or "").splitlines()]
        out: list[str] = []
        for line in lines:
            if not line.strip() and (not out or not out[-1].strip()):
                continue
            out.append(line)
        return "\n".join(out).strip()

    @staticmethod
    def _strip_source_links(value: str) -> str:
        """Keep Google AI prose/markdown but remove provider link clutter.

        SearchAPI may embed markdown/html/bare URLs inside the generated markdown.
        Sources are presented separately only when the user explicitly enables
        them, so the default answer stays readable and compact.
        """
        text = str(value or "")
        # Markdown links: keep human-readable label, discard URL.
        text = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]+\)", r"\1", text, flags=re.I)
        # HTML anchors: keep their visible text.
        text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)
        # Reference definitions and link-only bullets.
        lines: list[str] = []
        source_heading_seen = False
        for raw in text.splitlines():
            line = raw.rstrip()
            if re.match(r"^\s*\[[^\]]+\]:\s*(?:https?://|www\.)", line, re.I):
                continue
            if re.match(r"^\s*(?:#{1,6}\s*)?(?:источники|sources|references|ссылки)\s*:??\s*$", line, re.I):
                source_heading_seen = True
                continue
            if source_heading_seen:
                if not line.strip() or re.search(r"(?:https?://|www\.)", line, re.I):
                    continue
                # If provider appended a sources section without URLs, do not leak
                # it as duplicate trailing metadata either.
                if re.match(r"^\s*[-*•]?\s*\d*[.)]?\s*[^.!?]{0,160}$", line):
                    continue
                source_heading_seen = False
            if re.match(r"^\s*[-*•]?\s*(?:https?://|www\.)\S+\s*$", line, re.I):
                continue
            # Remove remaining bare URLs while preserving surrounding sentence text.
            cleaned, substitutions = re.subn(r"(?:https?://|www\.)\S+", "", line, flags=re.I)
            line = cleaned.rstrip(" -–—:;,.()[]") if substitutions else cleaned
            lines.append(line)
        out: list[str] = []
        for line in lines:
            if not line.strip() and (not out or not out[-1].strip()):
                continue
            out.append(line)
        return "\n".join(out).strip()

    def _present_answer(self, raw_answer: str, sources: list[dict[str, Any]], *, general: bool = False) -> str:
        answer = self._strip_source_links(self._clean_provider_answer(raw_answer))
        rows: list[str] = []
        for row in list(sources or [])[:10]:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            title = str(row.get("title") or row.get("domain") or url).strip()
            rows.append(f"- {title}: {url}")
        lang = str(self.config.get("app.language", "ru") or "ru")
        labels = {
            "ru": ("Данные поиска SearchAPI.io", "Источники", "Источник: карточка Google через SearchAPI.io; отдельная ссылка не предоставлена."),
            "en": ("SearchAPI.io search data", "Sources", "Source: Google answer card via SearchAPI.io; no separate link was supplied."),
            "de": ("Suchdaten von SearchAPI.io", "Quellen", "Quelle: Google-Antwortkarte über SearchAPI.io; kein separater Link verfügbar."),
            "es": ("Datos de búsqueda de SearchAPI.io", "Fuentes", "Fuente: tarjeta de Google mediante SearchAPI.io; sin enlace independiente."),
            "fr": ("Résultats de recherche SearchAPI.io", "Sources", "Source : fiche Google via SearchAPI.io ; aucun lien distinct fourni."),
            "it": ("Risultati di ricerca SearchAPI.io", "Fonti", "Fonte: scheda Google tramite SearchAPI.io; nessun link separato fornito."),
            "pt": ("Resultados da pesquisa SearchAPI.io", "Fontes", "Fonte: cartão do Google via SearchAPI.io; nenhum link separado fornecido."),
        }
        prefix, heading, fallback = labels.get(lang, labels["en"])
        # UI185-API-SOURCE-LINK-TOGGLE-COPY-01: the switch covers general web
        # answers (structured or full); game answers always list their pages, as
        # the setting's help says. Attribution (the prefix) is never removed.
        if rows and (not general or bool(self.config.get("gaming.web_knowledge.show_sources", False))):
            suffix = heading + ":\n" + "\n".join(rows)
        elif rows:
            suffix = ""
        else:
            suffix = fallback
        return prefix + ":\n\n" + answer.rstrip() + ("\n\n" + suffix if suffix else "")

    def _present_general171(self, question: str, raw_answer: str, sources: list[dict[str, Any]], related: list[str]) -> str:
        """NEO 171: a short lead and the key points for this question, the source, then an offer.

        Only the provider's own sentences are selected (never rephrased); the full answer
        stays available by «подробнее». The offer's reply («да», «отзывы», «2018»...) runs
        the next search (jarvis.runtime.web_answers171.resolve).
        """
        from jarvis.language import language_code
        from jarvis.runtime import web_answers171
        if not bool(self.config.get("gaming.web_knowledge.structured_answers", True)):
            return self._present_answer(raw_answer, sources, general=True)
        code = language_code(self.config)
        full = self._strip_source_links(self._clean_provider_answer(raw_answer))
        body, shortened = web_answers171.structure(question, full, code)
        rows = []
        for row in list(sources or [])[:10]:
            url = str(row.get("url") or "").strip()
            if url:
                rows.append(f"- {str(row.get('title') or row.get('domain') or url).strip()}: {url}")
        labels = {
            "ru": ("Нашёл через SearchAPI.io (Google)", "Источники"), "en": ("Found via SearchAPI.io (Google)", "Sources"),
            "de": ("Gefunden über SearchAPI.io (Google)", "Quellen"), "fr": ("Trouvé via SearchAPI.io (Google)", "Sources"),
            "es": ("Encontrado con SearchAPI.io (Google)", "Fuentes"), "it": ("Trovato con SearchAPI.io (Google)", "Fonti"),
            "pt": ("Encontrado via SearchAPI.io (Google)", "Fontes"),
        }
        prefix, heading = labels.get(code, labels["en"])
        parts = [prefix + ":", body.rstrip()]
        if shortened:
            parts.append(web_answers171.more_hint(code))
        if rows and bool(self.config.get("gaming.web_knowledge.show_sources", False)):
            parts.append(heading + ":\n" + "\n".join(rows))
        elif not rows:
            # SQ-03: say honestly that the fragment has no separate page link.
            no_link = {
                "ru": "Источник: карточка Google через SearchAPI.io; отдельная ссылка не предоставлена.",
                "en": "Source: Google answer card via SearchAPI.io; no separate link was supplied.",
                "de": "Quelle: Google-Antwortkarte über SearchAPI.io; kein separater Link verfügbar.",
                "fr": "Source : fiche Google via SearchAPI.io ; aucun lien distinct fourni.",
                "es": "Fuente: tarjeta de Google mediante SearchAPI.io; sin enlace independiente.",
                "it": "Fonte: scheda Google tramite SearchAPI.io; nessun link separato fornito.",
                "pt": "Fonte: cartão do Google via SearchAPI.io; nenhum link separado fornecido.",
            }
            parts.append(no_link.get(code, no_link["en"]))
        items = web_answers171.offers(question, code) if bool(self.config.get("gaming.web_knowledge.suggest_followup", True)) else []
        offer = web_answers171.offer_text(items, code)
        if not offer:
            related_question = self._suggest_followup(question, full, related)
            if related_question:
                items = [web_answers171.related_offer(related_question)]
                offer = web_answers171.offer_text(items, code)
        web_answers171.remember(question, items, code, full=full if shortened else "")
        if offer:
            parts.append(offer)
        return "\n\n".join(parts)

    def _suggest_followup(self, question: str, answer: str, related: list[str]) -> str:
        """Use provider-supplied related questions only.

        V7.4 launched a local Qwen generation after every successful web answer just
        to invent a follow-up prompt. Besides adding CPU load and latency, that made
        the response path less deterministic. V7.6 never runs a second model here.
        """
        if not bool(self.config.get("gaming.web_knowledge.suggest_followup", True)):
            return ""
        from jarvis.runtime.web_relevance186 import related_is_useful
        for item in related:
            q = re.sub(r"\s+", " ", str(item or "")).strip()
            # SQ-04: same topic, something new — never an off-topic provider
            # string and never the question that was just answered.
            if q and len(q) <= 220 and norm(q) != norm(question) and related_is_useful(q, question, answer):
                return q if q.endswith("?") else q + "?"
        return ""

    @staticmethod
    def _append_invitation(answer: str, pending: str) -> str:
        if not pending:
            return answer
        return f"{answer.rstrip()}\n\nХотите узнать: {pending.rstrip('?')}?"

    def _remember_conversation(
        self, game: dict[str, Any], question: str, search_query: str, answer: str,
        provider: str, continuation_token: str, related: list[str], pending: str,
    ) -> None:
        with self._lock:
            self._conversation = _Conversation(
                str(game.get("game_id") or "generic"), self._display_game_name(game),
                str(question or "").strip(), str(answer or "").strip(), str(search_query or "").strip(),
                str(provider or ""), str(continuation_token or ""), tuple(related[:5]), str(pending or ""), time.time(),
            )
            self._forced = _ForcedFollowup()

    def owns_general_web(self, text: str) -> bool:
        """Return True for external factual knowledge that belongs to Google AI."""
        claim = current_turn_claim()
        if claim.enforced:
            if claim.owner == "web_information":
                return True
            # Game knowledge has its own provenance/cache lane; every local owner
            # is excluded before active-game context or general-question heuristics.
            return False
        if self._local_domain_control(text) or looks_like_browser_search(text):
            return False
        # Never steal an actual game question; the game lane carries active-game
        # context and must remain the sole provenance path for gameplay facts.
        game_owned, _ = self.owns(text)
        if game_owned:
            return False
        t = norm(text)
        # Explicit internet verbs always request SearchAPI.
        if looks_like_web_lookup(t):
            return True
        # NEO 171: conversation stays with the local model; specifications, prices, reviews,
        # news, people and figures go to the web even without a question word.
        from jarvis.runtime import web_answers171
        kind = web_answers171.classify(text)
        if kind == "chat":
            return False
        if kind == "web":
            return True
        # General factual questions are web-grounded by default in V7.7. Exclude
        # conversational/self/translation shells that are better handled locally.
        if not looks_like_general_question(t):
            return False
        if re.search(r"^(?:что\s+(?:ты|у\s+тебя)|как\s+(?:ты|дела)|кто\s+ты|что\s+ты\s+умеешь)\b", t, re.I):
            return False
        if re.search(r"^(?:переведи|как\s+переводится|translate\b|how\s+do\s+you\s+say\b)", t, re.I):
            return False
        return True

    def try_general_answer(self, text: str, *, force: bool = False) -> tuple[bool, str, dict[str, Any]]:
        """``force``: a follow-up search the user accepted (the query is NEO's own wording)."""
        if not force and not self.owns_general_web(text):
            return False, "", {}
        if not bool(self.config.get("gaming.web_knowledge.enabled", True)):
            return True, self.unavailable_message("searchapi_disabled", general=True), {"intent": "general_web_disabled", "grounded": False}
        raw_query = str(text or "").strip()
        query = strip_browser_search_scaffolding(raw_query) if looks_like_web_lookup(raw_query) else raw_query
        query = query or raw_query
        game_id = "__general_web__"
        cached = self.cache.get(game_id, query)
        if cached:
            raw_answer = str(cached.get("answer") or "")
            sources = list(cached.get("sources") or [])
            grounded = cached.get("grounded")
            return True, self._present_general171(raw_query, raw_answer, sources, list(cached.get("related_questions") or [])), {
                "intent": "general_google_ai_cache", "cached": True,
                "provider": str(cached.get("provider") or ""), "sources": sources,
                # REC-SQ03: a hit restores the recorded support, never upgrades it.
                "grounded": bool(sources) if grounded is None else bool(grounded),
                "answer_kind": str(cached.get("answer_kind") or ""),
                "search_query": str(cached.get("search_query") or query),
            }
        language = str(self.config.get("app.language", "ru") or "ru")
        response = self.searcher.search(query, language=language)
        for attempt in list(response.attempts or []):
            logger.info(
                "WebIntelligence attempt provider=%s stage=%s status=%s bytes=%s results=%s error=%r detail=%r",
                attempt.get("provider", ""), attempt.get("stage", ""), attempt.get("status", 0),
                attempt.get("bytes", 0), attempt.get("results", 0),
                str(attempt.get("error", ""))[:180], str(attempt.get("detail", ""))[:220],
            )
        raw_answer = self._clean_provider_answer(response.direct_answer)
        from jarvis.runtime.web_relevance186 import is_echo
        if raw_answer and is_echo(raw_answer, raw_query):
            # SQ-03: a repeated question is not an answer and is never cached.
            raw_answer = ""
        if raw_answer:
            sources: list[dict[str, Any]] = []
            supporting = [str(x) for x in list((response.metadata or {}).get("answer_sources") or []) if str(x)]
            for r in (response.results if supporting else response.results[:10]):
                row = r.as_dict(); row["domain"] = self._domain(r.url)
                # SQ-03: only pages that carry the shown fragments are presented as
                # its sources; unrelated organic rows are not attribution.
                if supporting and r.url not in supporting:
                    continue
                if not supporting and (response.metadata or {}).get("answer_box_without_link"):
                    continue
                row["role"] = "answer_source"
                sources.append(row)
            presented = self._present_general171(raw_query, raw_answer, sources, list(response.related_questions or []))
            if not self._strip_source_links(raw_answer).strip():
                # A URL-only payload has nothing left after presentation.
                msg = self.unavailable_message("searchapi_answer_missing", general=True)
                return True, msg, {"intent": "general_google_ai_unavailable", "cached": False, "provider": response.provider,
                                   "sources": [], "grounded": False, "error": "searchapi_answer_missing", "search_query": query}
            self.cache.put(
                game_id, "Интернет", query, search_query=query, answer=raw_answer,
                sources=sources, provider=response.provider, related_questions=list(response.related_questions or [])[:5],
                grounded=bool(sources) or bool((response.metadata or {}).get("answer_box_without_link")),
                answer_kind=response.direct_answer_kind,
            )
            return True, presented, {
                "intent": "general_google_ai_search", "cached": False, "provider": response.provider,
                "sources": sources, "grounded": bool(sources) or bool((response.metadata or {}).get("answer_box_without_link")),
                "search_query": query, "answer_kind": response.direct_answer_kind,
            }
        error = str(response.error or "")
        msg = self.unavailable_message(error or "searchapi_no_results", general=True)
        return True, msg, {"intent": "general_google_ai_unavailable", "cached": False, "provider": response.provider, "sources": [], "grounded": False, "error": error, "search_query": query}

    def try_answer(self, text: str) -> tuple[bool, str, dict[str, Any]]:
        owned, game = self.owns(text)
        if not owned:
            return False, "", {}
        if not bool(self.config.get("gaming.web_knowledge.enabled", True)):
            return True, self.unavailable_message("searchapi_disabled"), {**game, "intent": "web_disabled", "grounded": False}
        if game.get("needs_game_title"):
            with self._lock:
                self._missing_game = (str(text or "").strip(), time.monotonic() + 90.0)
            question = self._text(("О какой игре речь? Назови игру — я проверю ответ на твой вопрос.",
                "Which game do you mean? Name it and I will look up your question.",
                "Welches Spiel meinst du? Nenne den Titel, dann suche ich die Antwort.",
                "De quel jeu parles-tu ? Donne son titre et je chercherai la réponse.",
                "¿De qué juego hablas? Dime el título y buscaré la respuesta.",
                "Di quale gioco parli? Dimmi il titolo e cercherò la risposta.",
                "De que jogo estás a falar? Diz o título e procurarei a resposta."))
            return True, question, {**game, "intent": "clarify_game", "grounded": False}
        game_id = str(game.get("game_id") or "generic")
        game_name = self._display_game_name(game)
        scope_reason = str(game.get("scope_reason") or "")
        cache_question = self._cache_question(text, game)
        cached = self.cache.get(game_id, cache_question)
        if cached:
            raw_answer = str(cached.get("answer") or "")
            sources = list(cached.get("sources") or [])
            clean_answer = self._strip_source_links(self._clean_provider_answer(raw_answer))
            display_answer = self._present_answer(raw_answer, sources)
            related = list(cached.get("related_questions") or [])
            pending = str(cached.get("suggested_followup") or "").strip() or self._suggest_followup(text, clean_answer, related)
            self._remember_conversation(
                game, text, str(cached.get("search_query") or ""), clean_answer,
                str(cached.get("provider") or ""), str(cached.get("continuation_token") or ""), related, pending,
            )
            logger.info("GameWeb cache-hit game=%s provider=%s query=%r", game_id, cached.get("provider"), cached.get("search_query"))
            return True, self._append_invitation(display_answer, pending), {
                **game, "intent": "google_ai_cache", "cached": True, "provider": str(cached.get("provider") or ""),
                "sources": sources, "grounded": True,
                "search_query": str(cached.get("search_query") or ""), "pending_followup": pending,
            }

        search_query, continuation_token = self._build_search_query(text, game, scope_reason)
        language = str(self.config.get("app.language", "ru") or "ru")
        response = self.searcher.search(search_query, language=language)
        for attempt in list(response.attempts or []):
            logger.info(
                "GameWeb web attempt provider=%s stage=%s status=%s bytes=%s direct=%s results=%s error=%r detail=%r",
                attempt.get("provider", ""), attempt.get("stage", ""), attempt.get("status", 0),
                attempt.get("bytes", 0), bool(attempt.get("direct", False)), attempt.get("results", 0),
                str(attempt.get("error", ""))[:180], str(attempt.get("detail", ""))[:220],
            )
        logger.info(
            "GameWeb GoogleAI game=%s provider=%s answer_chars=%d refs=%d query=%r",
            game_id, response.provider, len(response.direct_answer or ""), len(response.results), search_query[:260],
        )

        raw_answer = self._clean_provider_answer(response.direct_answer)
        if raw_answer:
            sources: list[dict[str, Any]] = []
            for r in response.results[:10]:
                row = r.as_dict(); row["domain"] = self._domain(r.url); sources.append(row)
            clean_answer = self._strip_source_links(raw_answer)
            display_answer = self._present_answer(raw_answer, sources)
            related = list(response.related_questions or [])[:5]
            pending = self._suggest_followup(text, clean_answer, related)
            self.cache.put(
                game_id, game_name, cache_question, search_query=search_query, answer=raw_answer,
                sources=sources, provider=response.provider, related_questions=related,
                continuation_token=response.continuation_token, suggested_followup=pending,
            )
            self._remember_conversation(
                game, text, search_query, clean_answer, response.provider,
                response.continuation_token, related, pending,
            )
            return True, self._append_invitation(display_answer, pending), {
                **game, "intent": "google_ai_search", "cached": False, "provider": response.provider,
                "sources": sources, "grounded": True, "search_query": search_query,
                "pending_followup": pending, "continuation": False,
            }

        stale = self.cache.get(game_id, cache_question, allow_stale=True)
        if stale:
            raw_answer = str(stale.get("answer") or "").strip()
            stale_sources = list(stale.get("sources") or [])
            clean_answer = self._strip_source_links(self._clean_provider_answer(raw_answer))
            display_answer = self._present_answer(raw_answer, stale_sources)
            related = list(stale.get("related_questions") or [])
            pending = str(stale.get("suggested_followup") or "").strip() or self._suggest_followup(text, clean_answer, related)
            self._remember_conversation(
                game, text, str(stale.get("search_query") or ""), clean_answer,
                str(stale.get("provider") or ""), str(stale.get("continuation_token") or ""), related, pending,
            )
            stale_notices = {
                "ru": "Не удалось обновить поиск. Показываю ранее сохранённые результаты; они могли устареть.",
                "en": "The search could not be refreshed. Showing previously saved results; they may be out of date.",
                "de": "Die Suche konnte nicht aktualisiert werden. Ich zeige zuvor gespeicherte Ergebnisse; sie könnten veraltet sein.",
                "fr": "La recherche n’a pas pu être actualisée. Voici les résultats enregistrés précédemment ; ils peuvent être obsolètes.",
                "es": "No se ha podido actualizar la búsqueda. Muestro los resultados guardados anteriormente; pueden estar desactualizados.",
                "it": "Non è stato possibile aggiornare la ricerca. Mostro i risultati salvati in precedenza; potrebbero non essere aggiornati.",
                "pt": "Não foi possível atualizar a pesquisa. Apresento os resultados guardados anteriormente; podem estar desatualizados.",
            }
            answer = stale_notices.get(language, stale_notices["en"]) + "\n\n" + display_answer
            return True, self._append_invitation(answer, pending), {
                **game, "intent": "google_ai_stale_cache", "cached": True, "cache_stale": True,
                "provider": str(stale.get("provider") or ""), "sources": stale_sources,
                "grounded": True, "search_query": str(stale.get("search_query") or ""),
            }

        failure_text = self.unavailable_message(str(response.error or ""))
        return True, failure_text, {
            **game, "intent": "google_ai_unavailable", "cached": False, "provider": response.provider,
            "grounded": False, "error": response.error, "search_query": search_query,
        }
