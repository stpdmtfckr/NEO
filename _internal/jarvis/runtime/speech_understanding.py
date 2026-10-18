# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Context-aware speech understanding between Whisper and the Agent.

This module intentionally does *not* try to rewrite arbitrary conversation.  It
repairs only high-confidence command slots (applications/media providers) and
explicit self-corrections.  The raw transcript remains available to the Agent's
existing adaptive layer and logs.

The design mirrors a smart-speaker ASR post-processor:

* compare a few ASR hypotheses when the STT worker produced them;
* use a dynamic local vocabulary (configured terms, Start Menu apps, running
  processes and recent NEO context);
* use cross-script phonetic similarity for names such as ``дискорт``/Discord;
* understand explicit repairs such as ``открой телеграм, нет, дискорд``;
* avoid touching free payloads such as song titles, messages, translations and
  file paths unless the user explicitly names an application/provider slot.
"""

from jarvis.runtime.spoken_surface155 import normalize_spoken
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from jarvis.adaptation.phonetics import combined_similarity
from jarvis.runtime.command_variations import command_vocabulary_score
from jarvis.runtime.dictionary import configured_terms, installed_application_terms
from jarvis.runtime.multilingual_commands import command_vocabulary_score_for_language

logger = logging.getLogger(__name__)

__all__ = ["SpeechUnderstandingEngine", "SpeechResolution", "SpeechCorrection"]

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Only command shapes where a computer-local entity is a constrained slot.  The
# entity resolver is deliberately not used for open-ended search/message payloads.
_RU_ENTITY_COMMAND = re.compile(
    r"^\s*(?:пожалуйста\s+)?(?:"
    r"открой|открыть|запусти|запустить|закрой|закрыть|покажи|показать|"
    r"переключи(?:сь)?|перейди|фокус(?:ируй)?|разверни|сверни|"
    r"поставь|приостанови|останови|продолжи|возобнови|включи|выключи"
    r")\b",
    re.I,
)
_RU_FIXED_MEDIA_ACTION = re.compile(
    r"^\s*(?:пожалуйста\s+)?(?:включи|включить|запусти|запустить|поставь|поставить|играй|вруби)\b",
    re.I,
)
_RU_APP_WINDOW_ACTION = re.compile(
    r"^\s*(?:пожалуйста\s+)?(?:открой|открыть|запусти|запустить|закрой|закрыть|покажи|показать|"
    r"переключи(?:сь)?|перейди|фокус(?:ируй)?|разверни|сверни)\b", re.I,
)
_EN_APP_WINDOW_ACTION = re.compile(
    r"^\s*(?:please\s+)?(?:open|launch|run|start|close|show|focus|switch|go)\b", re.I,
)

_EN_ENTITY_COMMAND = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"open|launch|run|start|close|show|focus|switch|go|pause|stop|resume|play|mute|unmute"
    r")\b",
    re.I,
)

# Strong free-text shells.  Inside these, spelling is user data and must not be
# silently rewritten into an installed application merely because it is similar.
_FREE_PAYLOAD = re.compile(
    r"^\s*(?:"
    r"переведи|перевести|напиши|скажи|отправь|сообщение|письмо|"
    r"найди\s+(?:песн\w*|трек\w*|музык\w*|видео|ролик|видос)|"
    r"включи\s+(?:песн\w*|трек\w*|композици\w*)|"
    r"поиск\w*|загугли|"
    r"translate|write|say|send|message|email|"
    r"search\s+(?:for\s+)?(?:song|track|video)|play\s+(?:song|track)|"
    r"übersetze|schreibe|sende|suche|spiel\w*\s+(?:lied|titel)|"
    r"traduis|écris|envoie|cherche|joue\s+(?:chanson|titre)|"
    r"traduce|escribe|envía|busca|reproduce\s+(?:canción|tema)|"
    r"traduci|scrivi|invia|cerca|riproduci\s+(?:canzone|brano)|"
    r"traduza|escreva|envie|procure|reproduza\s+(?:música|faixa)"
    r")\b",
    re.I,
)

_RISKY_SHELL = re.compile(r"^\s*(?:удали|удалить|закрой|закрыть|отправь|отправить|напиши|delete|remove|close|send|write|schließ\w*|schliess\w*|lösch\w*|sende|ferme|fermer|supprime|envoie|cierra|cerrar|elimina|envía|chiudi|invia|feche|fechar|exclua|envie)\b", re.I)

_FILE_PAYLOAD = re.compile(
    r"\b(?:файл\w*|папк\w*|документ\w*|path|file|folder|document|datei|ordner|fichier|dossier|archivo|carpeta|cartella|ficheiro|pasta)\b",
    re.I,
)

# Contextual slot ownership.  We do not globally fuzzy-correct nouns: a candidate
# must come from the kind of object the sentence is currently asking for.
_DISCORD_SLOT = re.compile(
    r"\b(?:discord|дискорд|дискорт|сервер\w*|канал\w*|voice|голосов\w*)\b", re.I
)
_TELEGRAM_SLOT = re.compile(
    r"\b(?:telegram|телеграм\w*|контакт\w*|получател\w*|адресат\w*|групп\w*)\b", re.I
)
_MEDIA_SLOT = re.compile(
    r"\b(?:музык\w*|трек\w*|плеер\w*|media|spotify|спотиф\w*|youtube|ютуб\w*|yandex|яндекс)\b", re.I
)
_GAME_SLOT = re.compile(r"\b(?:игр\w*|game|spiel|jeu|juego|gioco|jogo|стим|steam)\b", re.I)
_WINDOW_SLOT = re.compile(r"\b(?:окн\w*|window|fenster|fenêtre|ventana|finestra|janela|вкладк\w*|tab)\b", re.I)
_TELEGRAM_SEND = re.compile(
    r"^\s*(?:напиши|отправь|ответь|send|message|write|reply)\b", re.I
)
_FILE_COMMAND = re.compile(
    r"^\s*(?:пожалуйста\s+)?(?:открой|открыть|покажи|показать|прочитай|удали|удалить|"
    r"переименуй|переименовать|скопируй|копируй|перемести|создай|создать|найди\s+файл|"
    r"open|show|read|delete|remove|rename|copy|move|create|"
    r"öffne|oeffne|zeige|lies|lösche|loesche|benenne|kopiere|verschiebe|"
    r"ouvre|affiche|lis|supprime|renomme|copie|déplace|deplace|"
    r"abre|muestra|lee|elimina|borra|renombra|copia|mueve|"
    r"apri|mostra|leggi|elimina|rinomina|copia|sposta|"
    r"abra|mostre|leia|exclua|apague|renomeie|copie|mova)\b", re.I
)

# Message/search bodies are free text, but the *recipient* before these separators
# can still be resolved against an allow-listed Telegram person/group.
_PAYLOAD_SEPARATORS = re.compile(
    r"\b(?:что|текст|сообщение|message|text|saying|скажи)\b|[:,]", re.I
)

# Explicit repair markers are safe only when the left side already looks like a
# command.  This avoids turning normal conversation containing "нет" into a new
# command.
_REPAIR_MARKER = re.compile(
    r"\b(?:нет|точнее|вернее|ой|то\s+есть|я\s+хотел\s+сказать|"
    r"no|rather|actually|i\s+mean)\b",
    re.I,
)

_RU_ACTION = re.compile(
    r"^\s*((?:пожалуйста\s+)?(?:открой|открыть|запусти|запустить|закрой|закрыть|"
    r"покажи|показать|переключи(?:сь)?|перейди|разверни|сверни|поставь|"
    r"приостанови|останови|продолжи|возобнови|включи|выключи))\b",
    re.I,
)
_EN_ACTION = re.compile(
    r"^\s*((?:please\s+)?(?:open|launch|run|start|close|show|focus|switch|go|"
    r"pause|stop|resume|play|mute|unmute))\b",
    re.I,
)

_OTHER_ACTIONS: dict[str, re.Pattern[str]] = {
    "de": re.compile(r"^\s*((?:bitte\s+)?(?:öffne|oeffne|start(?:e|en)?|schließ(?:e|en)?|schliess(?:e|en)?|zeige|fokussiere|wechsel|pausiere|stoppe|setz\w*\s+fort|spiele))\b", re.I),
    "fr": re.compile(r"^\s*((?:s['’]il\s+te\s+plaît\s+)?(?:ouvre|ouvrir|lance|lancer|ferme|fermer|affiche|montre|passe|pause|arrête|arrete|reprends|joue))\b", re.I),
    "es": re.compile(r"^\s*((?:por\s+favor\s+)?(?:abre|abrir|inicia|iniciar|lanza|lanzar|cierra|cerrar|muestra|cambia|pausa|detén|deten|reanuda|reproduce))\b", re.I),
    "it": re.compile(r"^\s*((?:per\s+favore\s+)?(?:apri|avvia|chiudi|mostra|passa|cambia|pausa|ferma|riprendi|riproduci))\b", re.I),
    "pt": re.compile(r"^\s*((?:por\s+favor\s+)?(?:abra|abrir|inicie|iniciar|feche|fechar|mostre|mude|pausa|pause|pare|retome|reproduza))\b", re.I),
}

# App/window verbs are a narrower subset than media verbs in every language.
# Requiring the literal word "window" here prevented "öffne Telegram" and
# equivalent Spanish/French/Italian/Portuguese commands from using known names.
_OTHER_APP_WINDOW_ACTIONS = {
    "de": re.compile(r"^\s*(?:bitte\s+)?(?:öffne|oeffne|starte?|starten|schließe?|schliesse?|schließen|schliessen|zeige|fokussiere|wechsel)\b", re.I),
    "fr": re.compile(r"^\s*(?:s['’]il\s+te\s+plaît\s+)?(?:ouvre|ouvrir|lance|lancer|ferme|fermer|affiche|montre|passe)\b", re.I),
    "es": re.compile(r"^\s*(?:por\s+favor\s+)?(?:abre|abrir|inicia|iniciar|lanza|lanzar|cierra|cerrar|muestra|cambia)\b", re.I),
    "it": re.compile(r"^\s*(?:per\s+favore\s+)?(?:apri|avvia|chiudi|mostra|passa|cambia)\b", re.I),
    "pt": re.compile(r"^\s*(?:por\s+favor\s+)?(?:abra|abrir|inicie|iniciar|feche|fechar|mostre|mude)\b", re.I),
}

_PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "Discord": ("discord", "дискорд", "дискорт", "дискор", "diskord", "diskort"),
    "Telegram": ("telegram", "телеграм", "телеграмм", "телега", "telegam", "telegran"),
    "Steam": ("steam", "стим", "стеам", "стимм", "с ним"),
    "Spotify": ("spotify", "спотифай", "спотифи", "спотифэй"),
    "Yandex Music": (
        "yandex music", "яндекс музыка", "яндекс музыку", "яндекс мьюзик",
        "yandex muzyka", "андекс музыка",
    ),
    "YouTube": ("youtube", "ютуб", "ютьюб", "ютюб"),
    "Google Chrome": ("google chrome", "chrome", "хром", "гугл хром"),
    "Microsoft Edge": ("microsoft edge", "edge", "эдж", "майкрософт эдж"),
    "Opera": ("opera", "опера"),
    "Firefox": ("firefox", "фаерфокс", "файрфокс"),
    "OBS Studio": ("obs studio", "obs", "обс", "обс студио"),
    "Visual Studio Code": ("visual studio code", "vs code", "vscode", "ви эс код"),
}

_PROCESS_FRIENDLY: tuple[tuple[str, str], ...] = (
    ("discord", "Discord"),
    ("telegram", "Telegram"),
    ("steam", "Steam"),
    ("spotify", "Spotify"),
    ("yandexmusic", "Yandex Music"),
    ("yandex_music", "Yandex Music"),
    ("youtube", "YouTube"),
    ("chrome", "Google Chrome"),
    ("msedge", "Microsoft Edge"),
    ("opera", "Opera"),
    ("firefox", "Firefox"),
    ("obs64", "OBS Studio"),
    ("code", "Visual Studio Code"),
)

_PROCESS_SKIP = {
    "system", "registry", "smss", "csrss", "wininit", "services", "lsass",
    "svchost", "fontdrvhost", "dwm", "sihost", "taskhostw", "explorer",
    "runtimebroker", "searchhost", "startmenuexperiencehost", "textinputhost",
    "conhost", "dllhost", "audiodg", "ctfmon", "python", "pythonw",
}


def _norm(value: str) -> str:
    return re.sub(r"[\W_]+", " ", str(value or "").casefold(), flags=re.UNICODE).strip()


@dataclass(slots=True)
class _Entity:
    canonical: str
    aliases: tuple[str, ...]
    source: str
    active: bool = False
    recent: bool = False
    kind: str = "generic"


@dataclass(slots=True)
class SpeechCorrection:
    original: str
    replacement: str
    reason: str
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "replacement": self.replacement,
            "reason": self.reason,
            "confidence": round(float(self.confidence), 3),
        }


@dataclass(slots=True)
class SpeechResolution:
    raw: str
    text: str
    corrections: list[SpeechCorrection] = field(default_factory=list)
    selected_hypothesis: str = "primary"
    score: float = 0.0

    @property
    def changed(self) -> bool:
        return self.text.strip() != self.raw.strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "text": self.text,
            "changed": self.changed,
            "selected_hypothesis": self.selected_hypothesis,
            "score": round(float(self.score), 3),
            "corrections": [item.as_dict() for item in self.corrections],
        }


_JOURNAL_EXECUTOR186 = None


def _journal_executor186():
    global _JOURNAL_EXECUTOR186
    if _JOURNAL_EXECUTOR186 is None:
        from concurrent.futures import ThreadPoolExecutor
        _JOURNAL_EXECUTOR186 = ThreadPoolExecutor(max_workers=1, thread_name_prefix="NEO-speech-journal")
    return _JOURNAL_EXECUTOR186


def _safe_journal186(fn, payload) -> None:
    try:
        fn(payload)
    except Exception:
        logger.debug("SpeechUnderstanding resolution journal failed", exc_info=True)


class SpeechUnderstandingEngine:
    """Conservative smart-speaker-like resolver for voice transcripts."""

    def __init__(self, config: Any, *, agent: Any = None) -> None:
        self.config = config
        self.agent = agent
        self._snapshot_at = 0.0
        self._entities: list[_Entity] = []
        self._file_cache_key = ""
        self._file_cache_at = 0.0
        self._file_entities_cache: list[_Entity] = []
        self.last_resolution: SpeechResolution | None = None

    def _get(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _enabled(self) -> bool:
        return bool(self._get("voice.speech_understanding_enabled", True))

    def _context_state(self) -> dict[str, Any]:
        try:
            memory = getattr(self.agent, "context_memory", None)
            snapshot = getattr(memory, "state_snapshot", None)
            if callable(snapshot):
                value = snapshot()
                if isinstance(value, dict):
                    return value
        except Exception:
            logger.debug("SpeechUnderstanding context snapshot failed", exc_info=True)
        return {}

    @staticmethod
    def _friendly_process_name(stem: str) -> str:
        low = _norm(stem).replace(" ", "")
        for needle, friendly in _PROCESS_FRIENDLY:
            if needle in low:
                return friendly
        # Keep a useful executable name, but reject opaque/system-looking ones.
        clean = re.sub(r"[_-]+", " ", stem).strip()
        return clean if 3 <= len(clean) <= 48 else ""

    def _running_names(self, limit: int = 120) -> list[str]:
        if os.name != "nt":
            return []
        out: list[str] = []
        seen: set[str] = set()
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                try:
                    raw = str(proc.info.get("name", "") or "").strip()
                except Exception:
                    continue
                if not raw:
                    continue
                stem = Path(raw).stem.strip()
                key = _norm(stem).replace(" ", "")
                if not key or key in _PROCESS_SKIP:
                    continue
                friendly = self._friendly_process_name(stem)
                normed = _norm(friendly)
                if not normed or normed in seen:
                    continue
                seen.add(normed)
                out.append(friendly)
                if len(out) >= limit:
                    break
        except Exception:
            logger.debug("SpeechUnderstanding process vocabulary failed", exc_info=True)
        return out

    def _refresh_entities(self) -> list[_Entity]:
        refresh = max(3.0, float(self._get("voice.speech_understanding_refresh_seconds", 12.0) or 12.0))
        now = time.monotonic()
        if self._entities and now - self._snapshot_at < refresh:
            return self._entities
        import threading
        if threading.current_thread() is threading.main_thread() and os.name == "nt":
            # 186 (USER185-02): the Start Menu walk and the process scan never run
            # on the GUI thread. A stale snapshot answers at once while a single
            # background refresh prepares the next one; with no snapshot yet only
            # the in-memory sources (built-in, personal, context) are used.
            self._schedule_entity_refresh186()
            if self._entities:
                return self._entities
            return self._collect_entities186(now, scan=False)
        return self._collect_entities186(now, scan=True)

    def _schedule_entity_refresh186(self) -> None:
        import threading
        if getattr(self, "_entity_refresh_thread186", None) is not None and self._entity_refresh_thread186.is_alive():
            return

        def run() -> None:
            try:
                self._collect_entities186(time.monotonic(), scan=True)
            except Exception:
                logger.debug("SpeechUnderstanding background vocabulary refresh failed", exc_info=True)
        self._entity_refresh_thread186 = threading.Thread(target=run, name="NEO-speech-vocabulary", daemon=True)
        self._entity_refresh_thread186.start()

    def _collect_entities186(self, now: float, *, scan: bool) -> list[_Entity]:
        rows: dict[str, dict[str, Any]] = {}

        def add(
            canonical: str, aliases: Iterable[str] = (), *, source: str,
            active: bool = False, recent: bool = False, kind: str = "generic",
        ) -> None:
            name = str(canonical or "").strip()
            key = _norm(name)
            if len(key) < 2:
                return
            row = rows.setdefault(key, {
                "canonical": name, "aliases": set(), "source": source,
                "active": False, "recent": False, "kinds": {kind},
            })
            row["aliases"].add(name)
            for alias in aliases:
                if _norm(alias):
                    row["aliases"].add(str(alias).strip())
            row["active"] = bool(row["active"] or active)
            row["recent"] = bool(row["recent"] or recent)
            # Prefer explicit/personal labels over generic process-derived labels.
            if row["source"] == "process" and source != "process":
                row["source"] = source
            row.setdefault("kinds", set()).add(kind)

        for canonical, aliases in _PROVIDER_ALIASES.items():
            add(canonical, aliases, source="builtin", kind="media")
            add(canonical, aliases, source="builtin", kind="application")

        try:
            for item in configured_terms(self.config):
                add(str(item.get("name", "") or ""), item.get("aliases", ()) or (), source="personal", kind="personal")
        except Exception:
            logger.debug("SpeechUnderstanding personal vocabulary failed", exc_info=True)

        try:
            for name in (installed_application_terms(280) if scan else ()):
                add(name, source="installed", kind="application")
        except Exception:
            logger.debug("SpeechUnderstanding Start Menu vocabulary failed", exc_info=True)

        running = self._running_names() if scan else []
        running_norms = {_norm(x) for x in running}
        for name in running:
            add(name, source="process", active=True, kind="application")

        state = self._context_state()
        recent_names: list[str] = []
        for key in ("last_application", "last_game_name", "last_media_target"):
            value = str(state.get(key, "") or "").strip()
            if value:
                recent_names.append(value)
                kind = {"last_application": "application", "last_game_name": "game", "last_media_target": "media"}.get(key, "generic")
                add(value, source="context", active=_norm(value) in running_norms, recent=True, kind=kind)
        for value in state.get("recent_processes", []) if isinstance(state.get("recent_processes", []), list) else []:
            if str(value).strip():
                recent_names.append(str(value).strip())
        for name in recent_names[:24]:
            add(name, source="context", active=_norm(name) in running_norms, recent=True, kind="application")

        # Window/process context is already sampled by ContextMemory; consuming the
        # snapshot here is O(1) and does not add another desktop/process scan.
        try:
            memory = getattr(self.agent, "context_memory", None)
            system_snapshot = getattr(memory, "system_snapshot", None)
            system_state = system_snapshot() if callable(system_snapshot) else {}
            current = system_state.get("current_window") if isinstance(system_state, dict) else {}
            if isinstance(current, dict):
                proc = str(current.get("process", "") or "").strip()
                title = str(current.get("title", "") or "").strip()
                if proc:
                    add(self._friendly_process_name(Path(proc).stem) or Path(proc).stem, source="window", active=True, recent=True, kind="application")
                if 3 <= len(title) <= 96:
                    add(title, source="window", active=True, recent=True, kind="window")
        except Exception:
            logger.debug("SpeechUnderstanding window vocabulary failed", exc_info=True)

        # Discord names are taken only from explicit local configuration/context;
        # never query Discord/network on the speech hot path.
        try:
            profiles = self._get("integrations.discord.servers", []) or []
            if isinstance(profiles, list):
                for profile in profiles[:32]:
                    if not isinstance(profile, dict):
                        continue
                    name = str(profile.get("name", "") or "").strip()
                    if name:
                        add(name, source="discord", recent=_norm(name) == _norm(state.get("last_discord_server", "")), kind="discord_server")
                    for field_name in ("voice_channel", "music_text_channel"):
                        channel = str(profile.get(field_name, "") or "").strip()
                        if channel:
                            add(channel, source="discord", recent=_norm(channel) == _norm(state.get("last_discord_channel", "")), kind="discord_channel")
            for key, kind in (("integrations.discord.default_server", "discord_server"), ("integrations.discord.default_voice_channel", "discord_channel"), ("integrations.discord.music_text_channel", "discord_channel"), ("integrations.discord.music_control_channel", "discord_channel")):
                value = str(self._get(key, "") or "").strip()
                if value:
                    add(value, source="discord", recent=True, kind=kind)
            for key, kind in (("last_discord_server", "discord_server"), ("last_discord_channel", "discord_channel")):
                value = str(state.get(key, "") or "").strip()
                if value:
                    add(value, source="context", recent=True, kind=kind)
        except Exception:
            logger.debug("SpeechUnderstanding Discord vocabulary failed", exc_info=True)

        # Telegram outbound names are explicitly allow-listed by the user, so they
        # are safe contextual candidates. IDs/tokens are never inserted.
        try:
            people = self._get("integrations.telegram.outbound_allowed_people", []) or []
            groups = self._get("integrations.telegram.outbound_allowed_groups", []) or []
            if isinstance(people, str):
                people = [x.strip() for x in re.split(r"[;\n]+", people) if x.strip()]
            if isinstance(groups, str):
                groups = [x.strip() for x in re.split(r"[;\n]+", groups) if x.strip()]
            for value in list(people)[:80] if isinstance(people, list) else []:
                name = str(value or "").strip()
                if name:
                    add(name, source="telegram", kind="telegram_contact")
            for value in list(groups)[:80] if isinstance(groups, list) else []:
                raw_group = str(value or "").strip()
                name = raw_group.split("=", 1)[0].strip()
                if name:
                    add(name, source="telegram", kind="telegram_group")
        except Exception:
            logger.debug("SpeechUnderstanding Telegram vocabulary failed", exc_info=True)

        # Reuse the in-process active-game snapshot; no psutil/network work here.
        try:
            from jarvis.gaming.runtime import active_game_context
            game = active_game_context(max_age_seconds=60.0) or {}
            game_name = str(game.get("game_name", "") or "").strip()
            if game_name:
                aliases = [str(game.get("game_id", "") or "").strip()]
                add(game_name, [x for x in aliases if x], source="game", active=True, recent=True, kind="game")
        except Exception:
            logger.debug("SpeechUnderstanding game vocabulary failed", exc_info=True)

        # Recent object-model labels are free: tools already populated them after
        # successful actions.  They make follow-up speech robust without rescans.
        try:
            from jarvis.runtime.objects import desktop_objects
            for item in desktop_objects().snapshot(limit=32):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", "") or "").strip()
                kind = str(item.get("kind", "") or "").strip().casefold()
                mapped = {"application": "application", "window": "window", "discord": "discord_channel", "media": "media"}.get(kind, "generic")
                if label and mapped != "generic":
                    add(label, source="object_model", recent=True, kind=mapped)
        except Exception:
            logger.debug("SpeechUnderstanding object-model vocabulary failed", exc_info=True)

        # User-confirmed adaptive speech corrections are strong personalized
        # vocabulary.  Pull only active rules already resident in the local store.
        try:
            adaptive = getattr(self.agent, "adaptation", None)
            speech = getattr(adaptive, "speech", None)
            active_game = ""
            active_fn = getattr(self.agent, "_active_game_id", None)
            if callable(active_fn):
                active_game = str(active_fn() or "")
            lang = str(self._get("app.language", "ru") or "ru")
            rows_learned = speech.corrections(game_id=active_game, language=lang, limit=120) if speech is not None else []
            for row in rows_learned:
                if not isinstance(row, dict) or str(row.get("state", "") or "").casefold() != "active":
                    continue
                source_word = str(row.get("key", "") or "").strip()
                replacement = str(row.get("value", "") or "").strip()
                if replacement:
                    add(replacement, [source_word] if source_word else (), source="learned", recent=True, kind="learned")
        except Exception:
            logger.debug("SpeechUnderstanding adaptive vocabulary failed", exc_info=True)

        # Lifelong ambient vocabulary: words/entities that repeatedly occur in
        # the user's real conversations and work context become ASR candidates.
        # The store is local and cached; this adds no model/network call.
        try:
            ambient = getattr(self.agent, "ambient_memory", None)
            learned_rows = ambient.learned_terms(limit=96) if ambient is not None else []
            for row in learned_rows:
                if not isinstance(row, dict):
                    continue
                display = str(row.get("display", "") or "").strip()
                term = str(row.get("term", "") or "").strip()
                count = int(row.get("count", 0) or 0)
                confidence = float(row.get("confidence", 0.0) or 0.0)
                if display and count >= 2 and confidence >= 0.28:
                    add(display, [term] if term and _norm(term) != _norm(display) else (), source="ambient", recent=True, kind="learned")
        except Exception:
            logger.debug("SpeechUnderstanding ambient vocabulary failed", exc_info=True)

        self._entities = [
            _Entity(
                canonical=row["canonical"],
                aliases=tuple(sorted(row["aliases"], key=lambda x: (-len(_norm(x)), x.casefold()))),
                source=str(row["source"]),
                active=bool(row["active"]),
                recent=bool(row["recent"]),
                kind="|".join(sorted(str(x) for x in row.get("kinds", {"generic"}) if str(x))) or "generic",
            )
            for row in rows.values()
        ]
        # A quick in-memory list is not a full snapshot: keep it marked stale.
        self._snapshot_at = now if scan else 0.0
        return self._entities

    def _slot_kinds(self, text: str, language: str) -> set[str]:
        state = self._context_state()
        last_domain = _norm(state.get("last_domain", ""))
        last_integration = _norm(state.get("last_integration_target", ""))
        command_shape = self._is_command_shape(text, language)
        if _FILE_PAYLOAD.search(text):
            return {"file", "folder"} if _FILE_COMMAND.search(text) else set()
        if _TELEGRAM_SEND.search(text) and (_TELEGRAM_SLOT.search(text) or last_domain == "telegram messages"):
            return {"telegram_contact", "telegram_group"}
        if command_shape and (_DISCORD_SLOT.search(text) or last_domain == "discord" or last_integration == "discord"):
            return {"discord_server", "discord_channel", "application", "media"}
        if command_shape and _MEDIA_SLOT.search(text):
            # A media sentence contains mostly free payload (song/playlist names).
            # Only canonical media providers are constrained here; installed apps,
            # personal/ambient vocabulary must never rewrite the payload.
            return {"media"}
        if command_shape and (_GAME_SLOT.search(text) or last_domain == "gaming"):
            return {"game", "application"}
        if command_shape and _WINDOW_SLOT.search(text):
            return {"window", "application"}
        if command_shape:
            # Generic "play/enable/set" commands are open-ended.  Contextual
            # entity correction is safe only for verbs that explicitly target an
            # application/window, not for arbitrary nouns after «включи».
            code = str(language or "ru").casefold()
            if (code == "ru" and _RU_APP_WINDOW_ACTION.search(text)) or (code == "en" and _EN_APP_WINDOW_ACTION.search(text)):
                return {"application", "window"}
            if code not in {"ru", "en"}:
                pattern = _OTHER_APP_WINDOW_ACTIONS.get(code)
                if pattern and pattern.search(text):
                    return {"application", "window"}
            return set()
        return set()

    def _current_folder(self) -> Path | None:
        state = self._context_state()
        candidates = [str(state.get("last_folder_path", "") or "").strip()]
        try:
            memory = getattr(self.agent, "context_memory", None)
            snapshot = getattr(memory, "system_snapshot", None)
            system = snapshot() if callable(snapshot) else {}
            recent = system.get("recent_folders", []) if isinstance(system, dict) else []
            if isinstance(recent, list):
                candidates.extend(str(x or "").strip() for x in recent[:3])
        except Exception:
            pass
        for raw in candidates:
            if not raw:
                continue
            try:
                path = Path(raw)
                if path.is_dir():
                    return path
            except OSError:
                continue
        return None

    def _file_entities(self) -> list[_Entity]:
        folder = self._current_folder()
        if folder is None:
            return []
        try:
            stamp = f"{folder}|{folder.stat().st_mtime_ns}"
        except OSError:
            return []
        now = time.monotonic()
        if stamp == self._file_cache_key and now - self._file_cache_at < 8.0:
            return self._file_entities_cache
        out: list[_Entity] = []
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    name = str(entry.name or "").strip()
                    if not name or name.startswith("."):
                        continue
                    kind = "folder" if entry.is_dir(follow_symlinks=False) else "file"
                    aliases = [name]
                    if kind == "file":
                        stem = Path(name).stem.strip()
                        if len(stem) >= 2 and _norm(stem) != _norm(name):
                            aliases.append(stem)
                    out.append(_Entity(name, tuple(aliases), "current_folder", active=True, recent=True, kind=kind))
                    if len(out) >= 160:
                        break
        except OSError:
            out = []
        self._file_cache_key = stamp
        self._file_cache_at = now
        self._file_entities_cache = out
        return out

    @staticmethod
    def _telegram_target_limit(text: str) -> int | None:
        if not _TELEGRAM_SEND.search(text):
            return None
        hit = _PAYLOAD_SEPARATORS.search(text)
        return hit.start() if hit else len(text)

    def _apply_adaptive_resolution(self, text: str, language: str) -> tuple[str, SpeechCorrection | None]:
        # Learned corrections are valuable as ASR hints, but silently rewriting
        # an already-recognized sentence caused severe regressions (correct words
        # were replaced by unrelated recent vocabulary).  Keep direct rewriting
        # opt-in; confirmed terms can still be supplied to Whisper/rescue paths.
        if not bool(self._get("voice.speech_adaptive_rewrite_enabled", False)):
            return text, None
        adaptive = getattr(self.agent, "adaptation", None)
        if adaptive is None:
            return text, None
        try:
            active_fn = getattr(self.agent, "_active_game_id", None)
            active_game = str(active_fn() or "") if callable(active_fn) else ""
            result = adaptive.resolve(text, active_game=active_game, language=language)
            repaired = str(getattr(result, "text", text) or text).strip()
            if repaired and _norm(repaired) != _norm(text):
                return repaired, SpeechCorrection(text, repaired, "adaptive_confirmed", 0.98)
        except Exception:
            logger.debug("SpeechUnderstanding adaptive pre-chat resolution failed", exc_info=True)
        return text, None

    def contextual_rescue_terms(self, text: str, *, language: str = "ru", limit: int = 4) -> list[str]:
        """Return a few local names worth a conditional cheap ASR re-decode.

        This deliberately returns nothing for free-text payloads or already-clear
        matches. It is a *trigger hint*, not a correction: the second acoustic
        decode still has to hear a better form before the resolver can select it.
        """
        raw = str(text or "").strip()
        allowed = self._slot_kinds(raw, language)
        if not raw or not allowed:
            return []
        telegram_target_limit = self._telegram_target_limit(raw) if allowed & {"telegram_contact", "telegram_group"} else None
        if _FREE_PAYLOAD.search(raw) and telegram_target_limit is None:
            return []
        entities = list(self._refresh_entities())
        if allowed <= {"file", "folder"}:
            entities.extend(self._file_entities())
        words = list(_WORD_RE.finditer(raw))
        if len(words) < 2:
            return []
        action = self._action_prefix(raw, language)
        start_index = max(1, len(_WORD_RE.findall(action)))
        floor = max(0.50, min(0.80, float(self._get("voice.speech_understanding_rescue_floor", 0.58) or 0.58)))
        accept = max(0.72, min(0.97, float(self._get("voice.speech_understanding_entity_threshold", 0.82) or 0.82)))
        ranked: list[tuple[float, str]] = []
        for start in range(start_index, len(words)):
            for width in range(1, min(4, len(words) - start) + 1):
                first, last = words[start], words[start + width - 1]
                if telegram_target_limit is not None and last.end() > telegram_target_limit:
                    continue
                fragment = raw[first.start():last.end()]
                if len(_norm(fragment)) < 3:
                    continue
                for entity in entities:
                    kinds = {x for x in str(entity.kind or "generic").split("|") if x}
                    if entity.source in {"ambient", "learned"}:
                        continue
                    if "generic" not in kinds and not (kinds & allowed):
                        continue
                    if allowed == {"media"} and "media" not in kinds:
                        continue
                    score = self._entity_match(fragment, entity)
                    if entity.active:
                        score += 0.035
                    if entity.recent:
                        score += 0.025
                    score = min(1.0, score)
                    if floor <= score < accept and _norm(fragment) != _norm(entity.canonical):
                        ranked.append((score, entity.canonical))
        ranked.sort(key=lambda x: x[0], reverse=True)
        out: list[str] = []
        seen: set[str] = set()
        for score, name in ranked:
            key = _norm(name)
            if not key or key in seen:
                continue
            # Avoid weak random candidates unless they are active/recent enough to
            # have earned the score boost or are genuinely phonetically plausible.
            if score < floor:
                continue
            seen.add(key); out.append(name)
            if len(out) >= max(1, min(6, int(limit))):
                break
        return out

    def _record_resolution(self, result: SpeechResolution) -> None:
        self.last_resolution = result
        if not result.raw.strip():
            return
        try:
            memory = getattr(self.agent, "context_memory", None)
            fn = getattr(memory, "record_speech_resolution", None)
            if callable(fn):
                payload = result.as_dict()
                import threading
                if threading.current_thread() is threading.main_thread():
                    # 186 (USER185-02): the journal lock and the disk append stay
                    # off the GUI thread; the in-memory last_resolution is set above.
                    _journal_executor186().submit(_safe_journal186, fn, payload)
                else:
                    fn(payload)
        except Exception:
            logger.debug("SpeechUnderstanding resolution journal failed", exc_info=True)

    @staticmethod
    def _is_command_shape(text: str, language: str) -> bool:
        code = str(language or "ru").casefold()
        if code == "en":
            return bool(_EN_ENTITY_COMMAND.search(text))
        if code == "ru":
            return bool(_RU_ENTITY_COMMAND.search(text) or _EN_ENTITY_COMMAND.search(text))
        pattern = _OTHER_ACTIONS.get(code)
        return bool((pattern and pattern.search(text)) or _EN_ENTITY_COMMAND.search(text))

    @staticmethod
    def _action_prefix(text: str, language: str) -> str:
        code = str(language or "").casefold()
        if code == "en":
            patterns = (_EN_ACTION,)
        elif code == "ru" or not code:
            patterns = (_RU_ACTION, _EN_ACTION)
        else:
            extra = _OTHER_ACTIONS.get(code)
            patterns = tuple(x for x in (extra, _EN_ACTION) if x is not None)
        for pattern in patterns:
            hit = pattern.search(text)
            if hit:
                return hit.group(1).strip()
        return ""

    def _repair_literal_grammar161(self, text: str, language: str) -> tuple[str, SpeechCorrection | None]:
        """Use the same literal-word repair as typed requests, before entity ASR.

        A pending answer belongs to its requested slot. Ambiguous alternatives
        remain untouched here: Agent presents its existing clarification menu.
        """
        from jarvis.runtime.command_repair161 import candidates, repair
        if not candidates(text, language):
            return text, None
        from jarvis.runtime.feature_commands import parse_feature_command
        from jarvis.runtime.canonical_commands import parse_canonical_command
        from jarvis.runtime.short_requests160 import complete as short_complete
        from jarvis.runtime.dialogue_tasks import _partial
        def complete(value):
            result = parse_feature_command(value, language)
            if result is not None:
                return result
            registry = getattr(self.agent, 'tools', None)
            if registry is not None:
                native = parse_canonical_command(value, language, lambda name: registry.get(name).parameters)
                if native is not None and not native.error:
                    return native.tool, dict(native.arguments or {})
            return short_complete(value, language)
        def proposal(value):
            pending = _partial(value, language, complete, time.monotonic(), 60)
            if pending is not None:
                return pending.tool, pending.arguments, tuple((x.name,x.kind) for x in pending.slots)
            return complete(value)
        repaired, choices = repair(text, language, proposal)
        if repaired != text and not choices:
            # The established, bounded product-name repair also canonicalizes
            # a recovered «включи мая волна» before it reaches the user bubble.
            repaired, _ = self._repair_known_command_phrase(repaired, language)
            return repaired, SpeechCorrection(text, repaired, 'fixed_command_grammar161', 0.97)
        return text, None

    def _repair_known_command_phrase(self, text: str, language: str) -> tuple[str, SpeechCorrection | None]:
        """Repair a tiny whitelist of fixed product phrases after ASR.

        This correction is intentionally narrower than generic fuzzy matching.
        Only known media verbs plus a short list of observed/truncated renderings
        of «Моя волна» are accepted, so valid words such as «воля» are never
        rewritten just because they are lexically similar.
        """
        if str(language or "ru").casefold() != "ru" or not _RU_FIXED_MEDIA_ACTION.search(text):
            return text, None
        words = list(_WORD_RE.finditer(text))
        if len(words) < 3:
            return text, None
        action = self._action_prefix(text, language)
        start_index = max(1, len(_WORD_RE.findall(action)))
        safe_heads = {"моя", "мою", "мая", "маю"}
        safe_tails = {"вон", "вона", "вол", "волу", "волн", "волна", "волну"}
        correct_pairs = {("моя", "волна"), ("мою", "волну")}
        for index in range(start_index, len(words) - 1):
            first, second = words[index], words[index + 1]
            head = first.group(0).casefold().replace("ё", "е")
            tail = second.group(0).casefold().replace("ё", "е")
            if head not in safe_heads or tail not in safe_tails:
                continue
            if (head, tail) in correct_pairs:
                return text, None
            replacement = "мою волну" if head in {"мою", "маю"} else "моя волна"
            fragment = text[first.start():second.end()]
            repaired = text[:first.start()] + replacement + text[second.end():]
            return repaired, SpeechCorrection(fragment, replacement, "known_media_phrase", 0.985)
        return text, None

    def _repair_explicit_self_correction(self, text: str, language: str) -> tuple[str, SpeechCorrection | None]:
        if not bool(self._get("voice.speech_understanding_self_corrections", True)):
            return text, None
        if not self._is_command_shape(text, language):
            return text, None
        matches = list(_REPAIR_MARKER.finditer(text))
        if not matches:
            return text, None
        marker = matches[-1]
        left = text[:marker.start()].strip(" ,.;:—-")
        right = text[marker.end():].strip(" ,.;:—-")
        if len(_WORD_RE.findall(left)) < 2 or not right or len(_WORD_RE.findall(right)) > 10:
            return text, None
        # If the user restarts with a new command after the correction marker,
        # trust that complete suffix. Otherwise keep the original action shell.
        if self._is_command_shape(right, language):
            repaired = right
        else:
            action = self._action_prefix(left, language)
            if not action:
                return text, None
            repaired = f"{action} {right}".strip()
        if _norm(repaired) == _norm(text):
            return text, None
        return repaired, SpeechCorrection(text, repaired, "explicit_self_correction", 0.99)

    def _repair_false_start(self, text: str, language: str, entities: list[_Entity]) -> tuple[str, SpeechCorrection | None]:
        if not bool(self._get("voice.speech_understanding_self_corrections", True)):
            return text, None
        if not self._is_command_shape(text, language):
            return text, None
        # Whisper may preserve an audible cut-off as "телег... Discord".  Remove
        # only the unfinished fragment when the following token is a strong local
        # entity, never based on punctuation alone.
        pattern = re.compile(r"\b([a-zа-яё0-9]{2,})\s*(?:\.{2,}|…+|--+)\s+([a-zа-яё0-9][a-zа-яё0-9 ._-]{1,40})", re.I)
        hit = pattern.search(text)
        if not hit:
            return text, None
        successor = hit.group(2).strip().split()[0]
        best = 0.0
        for entity in entities:
            for alias in entity.aliases:
                best = max(best, combined_similarity(successor, alias))
        if best < 0.88:
            return text, None
        repaired = text[:hit.start(1)] + text[hit.start(2):]
        repaired = re.sub(r"\s{2,}", " ", repaired).strip()
        return repaired, SpeechCorrection(text, repaired, "false_start_repair", best)

    def _entity_match(self, fragment: str, entity: _Entity) -> float:
        best = 0.0
        for alias in entity.aliases:
            # Avoid comparing a one-word fragment to a long multi-word label unless
            # one of its explicit aliases is also one word.
            if abs(len(_norm(fragment).split()) - len(_norm(alias).split())) > 1:
                continue
            best = max(best, combined_similarity(fragment, alias))
        return best

    def _repair_entity_slot(self, text: str, language: str, entities: list[_Entity]) -> tuple[str, SpeechCorrection | None, float]:
        if not bool(self._get("voice.speech_understanding_contextual_entities", True)):
            return text, None, 0.0
        allowed_kinds = self._slot_kinds(text, language)
        if not allowed_kinds:
            return text, None, 0.0
        # Search/song/translation/message bodies stay verbatim. Telegram outbound
        # commands are the one exception: only their bounded recipient slot may
        # be resolved, never the following message body.
        telegram_target_limit = self._telegram_target_limit(text) if allowed_kinds & {"telegram_contact", "telegram_group"} else None
        if _FREE_PAYLOAD.search(text) and telegram_target_limit is None:
            return text, None, 0.0
        words = list(_WORD_RE.finditer(text))
        if len(words) < 2:
            return text, None, 0.0

        action = self._action_prefix(text, language)
        action_words = len(_WORD_RE.findall(action))
        start_index = max(1, action_words)
        threshold = max(0.72, min(0.97, float(self._get("voice.speech_understanding_entity_threshold", 0.82) or 0.82)))
        if allowed_kinds <= {"file", "folder"}:
            threshold = max(threshold, 0.86)
        elif allowed_kinds <= {"telegram_contact", "telegram_group"}:
            threshold = max(threshold, 0.88)
        margin = max(0.02, min(0.25, float(self._get("voice.speech_understanding_entity_margin", 0.07) or 0.07)))

        proposals: list[tuple[float, int, int, _Entity, str]] = []
        for start in range(start_index, len(words)):
            # Usually app/provider names are <= 4 spoken words.
            for width in range(1, min(4, len(words) - start) + 1):
                first, last = words[start], words[start + width - 1]
                fragment = text[first.start():last.end()]
                if telegram_target_limit is not None and last.end() > telegram_target_limit:
                    continue
                normalized_fragment = _norm(fragment)
                if len(normalized_fragment) < 3:
                    continue
                ranked: list[tuple[float, _Entity]] = []
                for entity in entities:
                    entity_kinds = {x for x in str(entity.kind or "generic").split("|") if x}
                    # Ambient/learned vocabulary is intentionally decoder context,
                    # never an autocorrect dictionary.  A correct acoustic word
                    # must not become whatever happened to be seen recently.
                    if entity.source in {"ambient", "learned"}:
                        continue
                    if "generic" not in entity_kinds and not (entity_kinds & allowed_kinds):
                        continue
                    if allowed_kinds == {"media"} and "media" not in entity_kinds:
                        continue
                    score = self._entity_match(fragment, entity)
                    if entity.active:
                        score += 0.035
                    if entity.recent:
                        score += 0.025
                    score = min(1.0, score)
                    if score >= threshold - (0.06 if entity.active or entity.recent else 0.0):
                        ranked.append((score, entity))
                if not ranked:
                    continue
                ranked.sort(key=lambda item: item[0], reverse=True)
                best_score, best_entity = ranked[0]
                second = ranked[1][0] if len(ranked) > 1 and _norm(ranked[1][1].canonical) != _norm(best_entity.canonical) else 0.0
                if second and best_score - second < margin:
                    continue
                if _norm(best_entity.canonical) == normalized_fragment:
                    # Exact recognized entity is useful for hypothesis scoring even
                    # though there is nothing to rewrite.
                    proposals.append((best_score, start, width, best_entity, fragment))
                    continue
                proposals.append((best_score, start, width, best_entity, fragment))

        if not proposals:
            return text, None, 0.0
        # Prefer confident matches and, on ties, longer phrases (Yandex Music over
        # just Yandex).  We apply only one contextual entity correction per turn.
        proposals.sort(key=lambda item: (item[0], item[2]), reverse=True)
        score, start, width, entity, fragment = proposals[0]
        if _norm(fragment) == _norm(entity.canonical):
            return text, None, score
        first, last = words[start], words[start + width - 1]
        repaired = text[:first.start()] + entity.canonical + text[last.end():]
        correction = SpeechCorrection(fragment, entity.canonical, f"context_entity:{entity.source}", score)
        return repaired, correction, score

    @staticmethod
    def _acoustic_score(row: dict[str, Any], *, primary: bool) -> float:
        try:
            score = float(row.get("score", 0.0))
        except Exception:
            score = 0.0
        # faster-whisper quality score is generally negative.  We only need a
        # relative contribution, and keep it bounded so local context cannot make
        # a very poor acoustic hypothesis win by a huge margin.
        bounded = max(-1.5, min(0.4, score))
        return bounded + (0.090 if primary else 0.0)

    def _candidate_score(self, text: str, *, acoustic: float, language: str, entity_score: float, corrections: list[SpeechCorrection]) -> float:
        try:
            vocab = command_vocabulary_score_for_language(text, str(language or "ru"))
        except Exception:
            vocab = command_vocabulary_score(text)
        score = acoustic + min(1.0, max(0.0, float(vocab))) * 0.30
        score += min(1.0, max(0.0, entity_score)) * 0.08
        if self._is_command_shape(text, language):
            score += 0.08
        if corrections:
            score += min(0.04, sum(max(0.0, c.confidence - 0.80) for c in corrections) * 0.08)
        return score

    @staticmethod
    def _hypothesis_rows(primary: str, alternatives: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [{"text": primary, "source": "primary", "score": 0.0, "primary": True}]
        seen = {_norm(primary)}
        for item in alternatives or ():
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "").strip()
            key = _norm(text)
            if not key:
                continue
            if key in seen:
                if key == _norm(primary):
                    try:
                        rows[0]["score"] = float(item.get("score", rows[0].get("score", 0.0)))
                        rows[0]["meta"] = item.get("meta", {})
                    except Exception:
                        pass
                continue
            seen.add(key)
            row = dict(item)
            row["text"] = text
            row["primary"] = False
            rows.append(row)
            if len(rows) >= 5:
                break
        return rows

    def resolve(
        self,
        text: str,
        *,
        alternatives: Iterable[dict[str, Any]] | None = None,
        language: str = "ru",
        record: bool = True,
    ) -> SpeechResolution:
        original_input = str(text or "").strip()
        raw = normalize_spoken(original_input, wake=str(self._get("wake_word.phrase", "NEO")))
        if not raw or not self._enabled():
            return SpeechResolution(raw=original_input, text=raw)

        # Pending names/text/paths/choices are data even when they resemble a
        # misspelled command. The Agent owns cancellation and new-command rules.
        conversations = getattr(self.agent, 'conversations', None)
        if conversations is not None:
            state = conversations.get(getattr(self.agent, '_conversation_key', 'desktop'))
            if getattr(state, 'pending_intent', None) is not None or getattr(self.agent, '_pending_context', None):
                return SpeechResolution(raw=original_input, text=original_input)
        repaired, grammar_correction = self._repair_literal_grammar161(raw, language)
        if grammar_correction:
            result = SpeechResolution(raw=original_input, text=repaired, corrections=[grammar_correction],
                score=self._candidate_score(repaired, acoustic=0.09, language=language, entity_score=0, corrections=[grammar_correction]))
            if record:
                self._record_resolution(result)
            logger.info("SpeechUnderstanding '%s' -> '%s' source=primary score=%.3f corrections=%s",
                        raw, result.text, result.score, [grammar_correction.reason])
            return result

        # Literal slots and destructive/outbound actions never accept a guessed
        # alternate transcript or a globally corrected target.
        if _TELEGRAM_SEND.search(raw) and self._telegram_target_limit(raw) is not None:
            candidate, correction, score = self._repair_entity_slot(raw, language, list(self._refresh_entities()))
            if correction and score >= 0.999:
                return SpeechResolution(raw=original_input, text=candidate, corrections=[correction])
            return SpeechResolution(raw=original_input, text=raw)
        if _FREE_PAYLOAD.search(raw) or _RISKY_SHELL.search(raw) or any(q in raw for q in ('"', '«', '»', '“', '”')):
            return SpeechResolution(raw=original_input, text=raw)
        hypothesis_rows = self._hypothesis_rows(raw, alternatives)
        action = self._action_prefix(raw, language)
        hypothesis_rows = [row for row in hypothesis_rows if row.get('primary') or
                           (action and self._action_prefix(str(row.get('text', '')), language) == action)]
        # Dynamic vocabulary collection may enumerate running processes and Start
        # Menu entries. Do that only for command shapes with an actual constrained
        # entity slot; ordinary questions/conversation stay O(text) on the hot path.
        needs_entities = any(
            bool(self._slot_kinds(str(row.get("text", "") or ""), language))
            for row in hypothesis_rows
        )
        entities = list(self._refresh_entities()) if needs_entities else []
        if needs_entities and any(_FILE_PAYLOAD.search(str(row.get("text", "") or "")) for row in hypothesis_rows):
            entities.extend(self._file_entities())
        candidates: list[SpeechResolution] = []
        primary_entity_score = 0.0
        primary_action = self._action_prefix(raw, language)
        for row in hypothesis_rows:
            candidate = str(row.get("text", "") or "").strip()
            if not candidate:
                continue
            corrections: list[SpeechCorrection] = []
            candidate, correction = self._repair_explicit_self_correction(candidate, language)
            if correction:
                corrections.append(correction)
            candidate, correction = self._repair_false_start(candidate, language, entities)
            if correction:
                corrections.append(correction)
            candidate, correction = self._repair_known_command_phrase(candidate, language)
            if correction:
                corrections.append(correction)
            candidate, correction, entity_score = self._repair_entity_slot(candidate, language, entities)
            if correction:
                corrections.append(correction)
            candidate, correction = self._apply_adaptive_resolution(candidate, language)
            if correction:
                corrections.append(correction)
            acoustic = self._acoustic_score(row, primary=bool(row.get("primary")))
            score = self._candidate_score(
                candidate,
                acoustic=acoustic,
                language=language,
                entity_score=entity_score,
                corrections=corrections,
            )
            if row.get("primary"):
                primary_entity_score = entity_score
            elif (
                primary_entity_score < 0.72 and entity_score >= 0.97
                and primary_action and self._action_prefix(str(row["text"]), language) == primary_action
                and str(row.get("source", "")) in {"same_language_rescue", "contextual_rescue"}
                and self._slot_kinds(raw, language) <= {"application", "window"}
                and not _FREE_PAYLOAD.search(raw)
                and self._acoustic_score(row, primary=False) >= -0.45
            ):
                # Only an actual second acoustic decode can propose the name.
                # The verb stays unchanged, an already-known primary name wins,
                # and weak/unrelated phonetics cannot gain a context bonus.
                raw_slot = raw[len(primary_action):].strip()
                alternative_slot = str(row["text"])[len(primary_action):].strip()
                if combined_similarity(raw_slot, alternative_slot) >= 0.69:
                    score += 0.45
            candidates.append(SpeechResolution(
                raw=original_input,
                text=candidate,
                corrections=corrections,
                selected_hypothesis=str(row.get("source", "alternate") or "alternate"),
                score=score,
            ))

        if not candidates:
            result = SpeechResolution(raw=original_input, text=raw)
            if record:
                self._record_resolution(result)
            return result
        candidates.sort(key=lambda item: item.score, reverse=True)
        best = candidates[0]
        primary = next((item for item in candidates if item.selected_hypothesis == "primary"), candidates[0])
        # An alternate ASR decode must win clearly; contextual correction of the
        # primary itself is allowed without this extra margin.
        if best.selected_hypothesis != "primary":
            required = max(0.03, min(0.30, float(self._get("voice.speech_understanding_hypothesis_margin", 0.09) or 0.09)))
            if best.score < primary.score + required:
                best = primary

        if record:
            self._record_resolution(best)
        if best.changed:
            logger.info(
                "SpeechUnderstanding '%s' -> '%s' source=%s score=%.3f corrections=%s",
                raw, best.text, best.selected_hypothesis, best.score,
                [item.reason for item in best.corrections],
            )
        return best
