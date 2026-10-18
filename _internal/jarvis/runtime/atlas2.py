# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""NEO JARVIS Phrase Atlas 2.0.

The Atlas is deliberately offline-first and dependency-light:

    Normalize -> Static/regex router -> Semantic vector matcher
              -> System/context hints -> existing local AI fallback

The semantic layer uses a compact feature-hashed local vector representation built
from the shipped command atlases.  It needs only NumPy, does not start a model,
does not use FAISS/USearch and never executes destructive/target-bearing commands
from similarity alone.
"""

from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import zlib
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from jarvis.runtime.domains import classify_domain
from jarvis.language import language_code
from jarvis.runtime.text_normalization import normalize_spoken_command
from jarvis.runtime.phrase_atlas import normalize_phrase_atlas

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AtlasIntent:
    intent: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    domain: str = "general"
    confidence: float = 0.0
    priority: int = 600
    source: str = "semantic"
    executable: bool = False
    negative_rules: tuple[str, ...] = ()
    game_id: str = ""
    requires_vision: bool = False
    matched_phrase: str = ""


@dataclass(frozen=True)
class AtlasContext:
    domain: str = "general"
    active_game_id: str = ""
    active_game_name: str = ""
    active_process: str = ""
    active_window: str = ""
    active_folder: str = ""


@dataclass(frozen=True)
class AtlasResolution:
    routed: Any | None = None
    semantic: AtlasIntent | None = None
    context: AtlasContext = AtlasContext()
    source: str = "fallback"


_NEGATION_RE = re.compile(
    r"(?:^|\s)(?:не|нет|не надо|не нужно|не хочу|не делай|не выполняй|dont|don't|do not|without)(?:\s|$)",
    re.I,
)

# Similarity alone may execute only actions that need no user payload and are
# safe/reversible/read-only. Everything else is merely an intent hint for the
# existing deterministic router/local AI. Exact/regex routing keeps the mature
# SecurityPolicy and confirmation layer.
_NOT_ANCHORS175 = frozenset(
    "you your yours you're youre re i im i'm am we us they he she it its do does did have has had with about from at by "
    "so and or not no yes up out off just very really whats what's s t ll ve d "
    "ты тебя тебе тобой твой твоя твои я меня мне мы нас вы вас он она они ну же ли бы у с о об по к от до за так вот "
    "очень просто уже еще ещё да нет "
    "du dich dir ich wir ihr er sie es und oder nicht ja nein so sehr "
    "tu te toi je moi nous vous il elle et ou ne pas oui non très "
    "tú tu te yo me nosotros y o no sí muy "
    "io ti noi voi lui lei e o non sì molto "
    "eu tu te você voce nós e ou não sim muito".split())

_SEMANTIC_EXECUTION_ALLOWLIST = {
    # Explicit provider transport/status actions with no required user payload.
    "yandex_music.status", "yandex_music.play", "yandex_music.pause",
    "yandex_music.stop", "yandex_music.next", "yandex_music.previous",
    "yandex_music.my_wave", "yandex_music.my_tracks",
    "yandex_music.shuffle_on", "yandex_music.shuffle_off",
    "yandex_music.api_status", "yandex_music.background_status", "yandex_music.queue_status",
    "spotify.status", "spotify.background_status", "spotify.play", "spotify.pause",
    "spotify.stop", "spotify.next", "spotify.previous",
    "music.status", "music.background_status", "music.play", "music.pause",
    "music.stop", "music.next", "music.previous", "music.my_wave", "music.my_tracks",
    "music.shuffle_on", "music.shuffle_off", "music.queue_status",
    "discord.music_queue_status", "discord.music_pause", "discord.music_resume",
    "discord.music_skip", "discord.music_previous", "discord.music_stop",
    "discord.background_status",
    "media.status", "media.play_pause", "media.play", "media.pause",
    "system.status", "system.top_processes", "system.open_task_manager",
    "system.list_power_plans", "system.get_power_plan",
    "hardware.status", "diagnostics.self_test", "diagnostics.latency",
    "diagnostics.plugins", "diagnostics.objects",
    "window.active", "window.list", "window.minimize", "window.maximize", "window.restore",
    "clipboard.current", "clipboard.list", "memory.list", "scenario.list",
    "game.status", "game.state", "game.session_info", "game.show_session",
    "screen.capture", "weather.summary", "weather.current", "weather.temperature",
    "weather.forecast", "weather.rain", "weather.precipitation", "weather.humidity",
    "weather.wind", "weather.sunrise", "weather.sunset",
    "date_info.date_info", "timer.list", "system_monitor.summary", "system_monitor.cpu",
    "system_monitor.gpu", "system_monitor.memory", "system_monitor.disks",
    "system_monitor.network", "google_calendar.status", "google_calendar.today",
    "google_calendar.tomorrow", "google_calendar.upcoming", "google_calendar.list",
    "google_calendar.all", "google_calendar.next", "telegram_messages.status",
    "telegram_messages.count", "telegram_messages.recent", "telegram_messages.latest",
    "briefing.summary", "briefing.startup_summary",
    "discord.music_repeat_track", "discord.music_repeat_playlist", "discord.music_repeat_off",
    "music.repeat_on", "music.repeat_track", "music.repeat_playlist", "music.repeat_off",
    "spotify.shuffle_on", "spotify.shuffle_off", "spotify.repeat_on", "spotify.repeat_track",
    "spotify.repeat_playlist", "spotify.repeat_off",
    "yandex_music.repeat_on", "yandex_music.repeat_track", "yandex_music.repeat_playlist", "yandex_music.repeat_off",
}

# Even an exact phrase from a generated language atlas is not enough for actions
# that need a payload/target.  The regex/static router still handles those because
# it extracts entities and runs the normal risk policy.
_EXACT_HASH_ALLOWLIST = set(_SEMANTIC_EXECUTION_ALLOWLIST) | {
    "audio.list_devices", "network.info", "filesystem.list_drives",
    "history.recent", "automation.cancel", "media.fullscreen", "media.theater",
}

# Stop words are used only for semantic coverage/extras checks. They never alter
# the text passed to real tools.
_FILLER_WORDS = {
    "джарвис", "jarvis", "пожалуйста", "плиз", "please", "мне", "для", "меня",
    "сейчас", "прямо", "давай", "ну", "можешь", "можете", "сделай", "сделать",
    "the", "a", "an", "me", "now", "could", "can", "you", "to",
}

# Lightweight semantic concept expansion.  This is intentionally tiny and lives
# in-process: it gives the compact local vectors useful synonym/stem awareness
# without shipping a resident transformer model or vector database.  Concepts
# are added as extra vector features; the original words are preserved too.
_SEMANTIC_CONCEPT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("computer", re.compile(r"^(?:комп(?:ьютер|а|у|ом|е)?|пк|систем\w*|system\w*|syst\w*|sistema\w*|computer|pc)$", re.I)),
    ("hardware", re.compile(r"^(?:желез\w*|аппарат\w*|hardware|gpu|cpu|видеокарт\w*|процессор\w*)$", re.I)),
    ("health_status", re.compile(r"^(?:состоян\w*|статус\w*|нагруз\w*|загруз\w*|загруж\w*|температур\w*|чувств\w*|status\w*|statut\w*|estado\w*|stato\w*|health|load)$", re.I)),
    ("show", re.compile(r"^(?:покаж\w*|вывед\w*|show|display|zeige\w*|affich\w*|montr\w*|muestr\w*|mostr\w*)$", re.I)),
    ("resume", re.compile(r"^(?:продолж\w*|возобнов\w*|доиграй\w*|resume|play)$", re.I)),
    ("pause", re.compile(r"^(?:пауз\w*|приостанов\w*|pause)$", re.I)),
    ("next", re.compile(r"^(?:следующ\w*|дальше|переключ\w*|next|skip)$", re.I)),
    ("previous", re.compile(r"^(?:предыдущ\w*|назад|previous|back)$", re.I)),
    ("music", re.compile(r"^(?:музык\w*|песн\w*|трек\w*|композиц\w*|music|song|track)$", re.I)),
    ("discord", re.compile(r"^(?:дискорд\w*|дискорт\w*|discord\w*|бот\w*)$", re.I)),
    ("window", re.compile(r"^(?:окн\w*|окошк\w*|window)$", re.I)),
    ("minimize", re.compile(r"^(?:сверн\w*|минимиз\w*|спряч\w*|minimi[sz]\w*)$", re.I)),
    ("maximize", re.compile(r"^(?:разверн\w*|максимиз\w*|maximi[sz]\w*)$", re.I)),
    ("diagnostics", re.compile(r"^(?:диагност\w*|проверк\w*|тест\w*|diagnostic\w*|test)$", re.I)),
    ("latency", re.compile(r"^(?:задерж\w*|латент\w*|latency|ping)$", re.I)),
    ("shutdown", re.compile(r"^(?:выключ\w*|выруб\w*|отключ\w*|shutdown|poweroff)$", re.I)),
    ("restart", re.compile(r"^(?:перезагруз\w*|перезапуст\w*|restart|reboot)$", re.I)),
    ("game", re.compile(r"^(?:игр\w*|game|gaming)$", re.I)),
    ("session", re.compile(r"^(?:сесси\w*|сеанс\w*|session)$", re.I)),
    ("screen", re.compile(r"^(?:экран\w*|скрин\w*|monitor|screen)$", re.I)),
)


def _concepts(tokens: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for name, pattern in _SEMANTIC_CONCEPT_PATTERNS:
            if pattern.match(token) and name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _norm(text: str) -> str:
    # Runtime inputs have already passed through NEO JARVIS command/language
    # normalization in Agent.handle_stream. Keep this path intentionally tiny.
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("ё", "е")
    value = re.sub(r"[^\w+.-]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _seed_norm(text: str) -> str:
    # Seed canonicalization is used only while building the shipped semantic
    # matrix (or a rare fallback rebuild), never on the per-command hot path.
    value = normalize_spoken_command(str(text or ""))
    value = normalize_phrase_atlas(value)
    return _norm(value)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w+-]+", _norm(text), flags=re.UNICODE)


def _action_id_parts(action_id: str) -> tuple[str, str]:
    value = str(action_id or "").strip()
    if "." not in value:
        return value, ""
    return value.split(".", 1)


def _feature_hash(feature: str, dimension: int) -> tuple[int, float]:
    raw = feature.encode("utf-8", errors="ignore")
    h = zlib.crc32(raw) & 0xFFFFFFFF
    idx = h % dimension
    sign = -1.0 if (h & 0x80000000) else 1.0
    return idx, sign


class CompactSemanticVectorizer:
    """Tiny local embedding-like vectorizer for command intent matching.

    Word unigrams/bigrams carry most of the weight. Character n-grams make the
    representation robust to ASR spelling noise. The vector is normalized so a
    dot product is cosine similarity. This stays fast for the ~1-3k shipped atlas
    phrases and avoids another resident neural model during normal desktop use.
    """

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = max(192, min(2048, int(dimension)))

    def encode_one(self, text: str) -> np.ndarray:
        normalized = _norm(text)
        vec = np.zeros(self.dimension, dtype=np.float32)
        if not normalized:
            return vec
        toks = _tokens(normalized)
        features: list[tuple[str, float]] = []
        for token in toks:
            features.append(("w:" + token, 2.35))
        for concept in _concepts(toks):
            # Concept features carry enough weight to bridge common paraphrases,
            # while lexical features still dominate and keep intent boundaries
            # sharp. This avoids broad LLM-like guessing on control commands.
            features.append(("s:" + concept, 3.10))
        for a, b in zip(toks, toks[1:]):
            features.append(("b:" + a + "_" + b, 1.45))
        compact = "_".join(toks)
        # Character features are intentionally lighter than lexical features.
        for n, weight in ((3, 0.24), (4, 0.27)):
            if len(compact) < n:
                continue
            # Cap very long payloads: semantic routing should model the command,
            # not spend time hashing an entire dictated message/path.
            limit = min(len(compact) - n + 1, 64)
            for i in range(limit):
                features.append((f"c{n}:" + compact[i:i+n], weight))
        for feature, weight in features:
            idx, sign = _feature_hash(feature, self.dimension)
            vec[idx] += sign * weight
        length = float(np.linalg.norm(vec))
        if length > 1e-8:
            vec /= length
        return vec

    def encode(self, texts: Iterable[str]) -> np.ndarray:
        rows = [self.encode_one(item) for item in texts]
        if not rows:
            return np.zeros((0, self.dimension), dtype=np.float32)
        return np.stack(rows, axis=0).astype(np.float32, copy=False)


@dataclass(frozen=True)
class _AtlasFileIdentity:
    path: Path
    stat: tuple[int, ...]
    sha256: str
    crc32: int


@dataclass(frozen=True)
class _AtlasMissingDependency:
    path: Path


_AtlasDependencyIdentity = _AtlasFileIdentity | _AtlasMissingDependency


class _AtlasDependencyTooLarge(OSError):
    pass


class PhraseAtlas2:
    """Shared Atlas 2.0 engine used by voice/text/Telegram agents."""

    def __init__(self, config: Any, *, context_memory: Any = None) -> None:
        self.config = config
        self.memory = context_memory
        self._lock = threading.RLock()
        # A missing semantic cache can take seconds to rebuild. Serialize those
        # builds separately so background preparation never holds the exact
        # routing lock, while concurrent semantic requests reuse one build.
        self._semantic_lock = threading.Lock()
        self._vectorizer = CompactSemanticVectorizer(
            int(self._cfg("routing.semantic_dimension", 384) or 384)
        )
        self._semantic_cache: dict[str, tuple[list[dict[str, Any]], np.ndarray]] = {}
        self._exact_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._settings_identity = self._settings_signature()
        self._semantic_generations: dict[str, tuple[_AtlasDependencyIdentity, ...] | None] = {}
        self._exact_generations: dict[str, tuple[_AtlasDependencyIdentity, ...] | None] = {}
        # Instance-owned and bounded to one parsed locale. Its content identity,
        # not a language-only lru_cache key, decides whether parsing can be reused.
        self._language_cache: tuple[str, _AtlasFileIdentity, dict[str, Any]] | None = None

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _settings_signature(self) -> tuple[Any, ...]:
        dimension = max(192, min(2048, int(self._cfg("routing.semantic_dimension", 384) or 384)))
        return (language_code(self.config), dimension,
                bool(self._cfg("routing.atlas2_enabled", True)),
                bool(self._cfg("routing.static_hash_enabled", True)),
                bool(self._cfg("routing.semantic_enabled", True)))

    @staticmethod
    def _file_stat(stat: os.stat_result) -> tuple[int, ...]:
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    @staticmethod
    def _open_dependency(path: Path) -> Any:
        if not stat.S_ISREG(path.stat().st_mode):
            raise OSError("Atlas dependency is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        stream = os.fdopen(os.open(path, flags), "rb")
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            stream.close()
            raise OSError("Atlas dependency is not a regular file")
        return stream

    @classmethod
    def _read_dependency(cls, path: Path, *, content: bool = False) -> tuple[bytes, _AtlasFileIdentity]:
        """Hash only a selected dependency; use bounded extra digest memory.

        JSON callers still need their existing full parse input. Hash-only Save
        checks and matrix checks keep just one 64 KiB chunk, never a second matrix.
        Detect observed replacement/writes during the read conservatively.
        """
        digest = hashlib.sha256()
        crc = 0
        chunks: list[bytes] = []
        limit = (4096 if path.suffix == ".version" else 32 * 1024 * 1024 if path.suffix == ".npy"
                 else 4 * 1024 * 1024 if path.name.endswith(".rows.json") else 8 * 1024 * 1024)
        with cls._open_dependency(path) as stream:
            before = cls._file_stat(os.fstat(stream.fileno()))
            if before[2] > limit:
                raise _AtlasDependencyTooLarge("Atlas dependency exceeds retention read cap")
            total = 0
            while block := stream.read(min(64 * 1024, limit + 1 - total)):
                total += len(block)
                if total > limit:
                    raise _AtlasDependencyTooLarge("Atlas dependency grew beyond retention read cap")
                digest.update(block)
                crc = zlib.crc32(block, crc)
                if content:
                    chunks.append(block)
            after = cls._file_stat(os.fstat(stream.fileno()))
        if before != after or after != cls._file_stat(path.stat()):
            raise OSError("Atlas dependency changed while reading")
        return b"".join(chunks), _AtlasFileIdentity(path, after, digest.hexdigest(), crc)

    @classmethod
    def _read_build_dependency(cls, path: Path) -> tuple[bytes, _AtlasFileIdentity | None]:
        try:
            return cls._read_dependency(path, content=True)
        except OSError:
            # Identity failure must not shrink the corpus accepted by the old
            # regular-file loader. The original build-only read may still work;
            # its result is never eligible for retention. Save never calls it.
            with cls._open_dependency(path) as stream:
                return stream.read(), None

    @classmethod
    def _dependency_state(cls, path: Path) -> _AtlasDependencyIdentity | None:
        try:
            return cls._read_dependency(path)[1]
        except FileNotFoundError:
            return _AtlasMissingDependency(path)
        except Exception:
            return None

    @classmethod
    def _generation_is_current(cls, generation: tuple[_AtlasDependencyIdentity, ...]) -> bool:
        return all(cls._dependency_state(item.path) == item for item in generation)

    @classmethod
    def _mapped_payload_matches(cls, path: Path, matrix: np.ndarray) -> bool:
        # np.load opens the pathname itself. Also bind the mapped payload to that
        # file's observed bytes to reject a different mapping after replacement.
        # No array copy: compare one file chunk against a view of the mapping.
        try:
            if (not isinstance(matrix, np.memmap) or not matrix.flags.c_contiguous
                    or matrix.nbytes > 32 * 1024 * 1024):
                return False
            view = memoryview(matrix).cast("B")
            with cls._open_dependency(path) as stream:
                stream.seek(matrix.offset)
                for start in range(0, len(view), 64 * 1024):
                    block = stream.read(min(64 * 1024, len(view) - start))
                    if len(block) != min(64 * 1024, len(view) - start) or block != view[start:start + len(block)]:
                        return False
                return stream.read(1) == b""
        except Exception:
            return False

    def compatible_with(self, config: Any, *, context_memory: Any = None) -> bool:
        """Prove compatibility at Settings Save without waiting for a build.

        A cold/unknown generation or a busy cache is conservatively replaced.
        Threshold and margin deliberately stay live through the same config owner.
        """
        if config is not self.config or context_memory is not self.memory:
            return False
        try:
            if self._settings_signature() != self._settings_identity:
                return False
        except Exception:
            return False
        # Build order is semantic -> exact; never wait for either on the UI caller.
        if not self._semantic_lock.acquire(blocking=False):
            return False
        try:
            if not self._lock.acquire(blocking=False):
                return False
            try:
                selected = self._settings_identity[0]
                dependencies: dict[Path, _AtlasDependencyIdentity] = {}
                for cache, generations in ((self._semantic_cache, self._semantic_generations),
                                           (self._exact_cache, self._exact_generations)):
                    for key in cache:
                        generation = generations.get(key)
                        if key != selected or not generation:
                            return False
                        for item in generation:
                            if item.path in dependencies and dependencies[item.path] != item:
                                return False
                            dependencies[item.path] = item
                if not dependencies:
                    return False
                if self._language_cache is not None:
                    code, identity, _ = self._language_cache
                    if code != selected or dependencies.get(identity.path) != identity:
                        return False
                return self._generation_is_current(tuple(dependencies.values()))
            finally:
                self._lock.release()
        finally:
            self._semantic_lock.release()

    @staticmethod
    def _bounded_locale_cache_put(cache: dict[str, Any], key: str, value: Any, *, limit: int = 1) -> None:
        """Keep only the selected/recent locale matrices in memory.

        Python dicts preserve insertion order, so popping the first key gives a
        tiny LRU-like policy without another worker, timer or dependency.
        """
        if key in cache:
            cache.pop(key, None)
        cache[key] = value
        while len(cache) > max(1, int(limit)):
            oldest = next(iter(cache))
            if oldest == key and len(cache) > 1:
                oldest = next(k for k in cache if k != key)
            cache.pop(oldest, None)

    @staticmethod
    def _commands_path(language: str) -> Path:
        return Path(__file__).resolve().parents[1] / "locales" / "commands" / f"{language}.json"

    def enabled(self) -> bool:
        return bool(self._cfg("routing.atlas2_enabled", True))

    def semantic_enabled(self) -> bool:
        return self.enabled() and bool(self._cfg("routing.semantic_enabled", True))


    def _load_language_generation(self, language: str) -> tuple[dict[str, Any], _AtlasFileIdentity | None]:
        if language not in {"ru", "en", "de", "fr", "es", "it", "pt"}:
            language = "ru"
        path = self._commands_path(language)
        try:
            raw, identity = self._read_build_dependency(path)
            with self._lock:
                cached = self._language_cache
                if cached is not None and cached[0] == language and cached[1] == identity:
                    return cached[2], identity
                data = json.loads(raw.decode("utf-8"))
                data = data if isinstance(data, dict) else {}
                self._language_cache = (language, identity, data) if identity is not None else None
                return data, identity
        except Exception:
            return {}, None

    def _load_language_file(self, language: str) -> dict[str, Any]:
        return self._load_language_generation(language)[0]

    def _seed_rows(self, language: str, *, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        # Keep one tiny semantic matrix per supported command language. Static
        # routing still uses the mature internal Russian surface, while semantic
        # matching can compare the *raw* utterance with native Atlas examples.
        # This avoids making the flexible layer depend on perfect translation of
        # every paraphrase before routing.
        code = str(language or "ru").strip().casefold()
        if code not in {"ru", "en", "de", "fr", "es", "it", "pt"}:
            code = "ru"
        langs = [code]
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for code in langs:
            if data is None:
                data = self._load_language_file(code)
            actions = data.get("action_phrases", {}) if isinstance(data, dict) else {}
            if not isinstance(actions, dict):
                continue
            # HOTFIX103: exact phrase collisions are never useful semantic seeds.
            # A score-margin check can reject many ambiguous embeddings, but an
            # identical seed owned by two actions has zero lexical evidence for
            # choosing either action. Build a phrase->owners table first and drop
            # cross-action duplicates for every language, including future corpus
            # regenerations. Russian HOTFIX103 data is additionally collision-free
            # on disk, so this is a permanent second line of defence.
            phrase_owners: dict[str, set[str]] = {}
            for seed_action_id, seed_phrases in actions.items():
                if not isinstance(seed_phrases, list):
                    continue
                for seed_phrase in seed_phrases:
                    seed_normalized = _norm(str(seed_phrase or ""))
                    if seed_normalized:
                        phrase_owners.setdefault(seed_normalized, set()).add(str(seed_action_id))

            for action_id, phrases in actions.items():
                tool, action = _action_id_parts(str(action_id))
                if not tool or not action:
                    continue
                decision = classify_domain(tool, intent=str(action_id), arguments={"action": action})
                for phrase in phrases if isinstance(phrases, list) else []:
                    # Native command files are already reviewed command examples;
                    # preserve their language instead of canonicalizing them into
                    # the Russian static phrase surface.
                    normalized = _norm(str(phrase or ""))
                    if not normalized or len(phrase_owners.get(normalized, ())) > 1:
                        continue
                    key = (str(action_id), normalized)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append({
                        "intent": str(action_id), "tool": tool, "action": action,
                        "arguments": {"action": action}, "domain": decision.domain,
                        "phrase": normalized, "language": code,
                        "executable": str(action_id) in _SEMANTIC_EXECUTION_ALLOWLIST,
                    })
        # Curated additions cover HOTFIX89/Alpha-Beta actions newer than the
        # generated language corpus and common conversational wording. They are
        # Russian because the internal normalized fallback surface is Russian.
        if code == "ru":
            rows.extend(self._curated_rows())
        return rows

    def _curated_rows(self) -> list[dict[str, Any]]:
        specs = [
            ("discord.music_queue_status", "discord", "music_queue_status", "media", True,
             ["что сейчас играет в discord", "покажи очередь discord", "что дальше в очереди discord"]),
            ("discord.music_pause", "discord", "music_pause", "media", True,
             ["поставь discord музыку на паузу", "приостанови музыкального бота"]),
            ("discord.music_resume", "discord", "music_resume", "media", True,
             ["продолжи музыку discord", "возобнови музыкального бота"]),
            ("discord.music_skip", "discord", "music_skip", "media", True,
             ["пропусти трек в discord", "следующий трек музыкального бота"]),
            ("window.minimize", "window", "minimize", "system", True,
             ["убери текущее окно вниз", "спрячь активное окно", "минимизируй текущее окно"]),
            ("window.maximize", "window", "maximize", "system", True,
             ["раскрой активное окно", "разверни текущее окно полностью"]),
            ("window.restore", "window", "restore", "system", True,
             ["верни размер активного окна", "восстанови текущее окно"]),
            ("game.session_info", "game", "session_info", "gaming", True,
             ["где мы остановились в игре", "что сейчас с игровой сессией", "сколько я уже играю"]),
            ("game.show_session", "game", "show_session", "gaming", True,
             ["покажи игровую сессию", "выведи панель текущей игры"]),
            ("system.status", "system", "status", "system", True,
             ["как себя чувствует компьютер", "что сейчас с компьютером", "покажи состояние системы"]),
            ("hardware.status", "hardware", "status", "system", True,
             ["что по железу", "покажи температуры и нагрузку", "как чувствует себя железо"]),
            # Destructive commands are intentionally hints only.
            ("system.shutdown", "system", "shutdown", "system", False,
             ["выключи компьютер", "заверши работу компьютера", "выруби пк"]),
            ("system.restart", "system", "restart", "system", False,
             ["перезагрузи компьютер", "перезапусти windows"]),
            ("window.close", "window", "close", "system", False,
             ["закрой текущее окно", "убери это окно совсем"]),
            ("filesystem.delete", "filesystem", "delete", "system", False,
             ["удали этот файл", "сотри выбранный файл"]),
        ]
        rows: list[dict[str, Any]] = []
        for intent, tool, action, domain, executable, phrases in specs:
            for phrase in phrases:
                rows.append({
                    "intent": intent, "tool": tool, "action": action,
                    "arguments": {"action": action}, "domain": domain,
                    "phrase": _seed_norm(phrase), "language": "curated",
                    "executable": bool(executable),
                })
        return rows

    def prepare_semantic(self, language: str = "ru") -> None:
        """Prepare only the selected locale on the existing warm-up worker.

        This is local table preparation, with no model or command invocation.
        An early semantic request still waits for the shared build; static and
        exact routing stay available. Disabled semantic routing does no work.
        """
        if self.semantic_enabled():
            self._semantic_index(language)

    def _semantic_index(self, language: str) -> tuple[list[dict[str, Any]], np.ndarray]:
        key = str(language or "ru").strip().casefold()
        if key not in {"ru", "en", "de", "fr", "es", "it", "pt"}:
            key = "ru"
        with self._semantic_lock:
            cached = self._semantic_cache.get(key)
            if cached is not None:
                return cached
            cache_root = Path(__file__).resolve().parents[2] / "config" / "atlas2" / "cache"
            rows_path = cache_root / f"semantic_{key}.rows.json"
            matrix_path = cache_root / f"semantic_{key}.npy"
            stamp_path = cache_root / f"semantic_{key}.version"
            data, commands = self._load_language_generation(key)
            # Capture before attempting the shipped load, including its absence
            # or invalid content. A fallback must retry repaired/installed inputs
            # after Save rather than keep an older fallback on command identity alone.
            cache_before = tuple(self._dependency_state(path)
                                 for path in (stamp_path, rows_path, matrix_path))
            fallback_reusable = False
            try:
                stamp, stamp_identity = self._read_build_dependency(stamp_path)
                command_crc = commands.crc32 if commands is not None else zlib.crc32(self._read_build_dependency(self._commands_path(key))[0])
                expected = "unicode-v2:" + str(command_crc)
                if stamp.decode("utf-8").strip() != expected:
                    # A known stamp mismatch is stable only if it is the same
                    # content captured before this attempt, not a transient read.
                    fallback_reusable = stamp_identity is not None and stamp_identity == cache_before[0]
                    raise ValueError("Atlas cache does not match current locale")
                raw_rows, rows_identity = self._read_build_dependency(rows_path)
                rows = json.loads(raw_rows.decode("utf-8"))
                try:
                    matrix_identity = self._read_dependency(matrix_path)[1]
                except Exception:
                    matrix_identity = None
                matrix = np.load(matrix_path, allow_pickle=False, mmap_mode="r")
                if (
                    isinstance(rows, list) and matrix.ndim == 2
                    and matrix.shape[0] == len(rows)
                    and matrix.shape[1] == self._vectorizer.dimension
                ):
                    if matrix.dtype != np.float32:
                        raise ValueError("Atlas semantic cache must use float32")
                    generation = (commands, stamp_identity, rows_identity, matrix_identity)
                    ready = generation if (all(item is not None for item in generation)
                                           and cache_before == generation[1:]
                                           and self._mapped_payload_matches(matrix_path, matrix)
                                           and self._generation_is_current(generation)) else None
                    self._bounded_locale_cache_put(self._semantic_cache, key, (rows, matrix))
                    self._bounded_locale_cache_put(self._semantic_generations, key, ready)
                    return rows, matrix
            except Exception as exc:
                if isinstance(exc, FileNotFoundError) and exc.filename is not None:
                    fallback_reusable = any(isinstance(item, _AtlasMissingDependency)
                                            and item.path == Path(exc.filename) for item in cache_before)
                logger.debug("Atlas2 precomputed semantic cache unavailable; rebuilding", exc_info=True)
            rows = self._seed_rows(key, data=data)
            matrix = self._vectorizer.encode(row.get("phrase", "") for row in rows)
            generation = ((commands, *cache_before) if fallback_reusable and commands is not None
                          and all(item is not None for item in cache_before) else ())
            ready = generation if generation and self._generation_is_current(generation) else None
            self._bounded_locale_cache_put(self._semantic_cache, key, (rows, matrix))
            self._bounded_locale_cache_put(self._semantic_generations, key, ready)
            return rows, matrix

    def _exact_index(self, language: str) -> dict[str, dict[str, Any]]:
        key = str(language or "ru").strip().casefold() or "ru"
        with self._lock:
            cached = self._exact_cache.get(key)
            if cached is not None:
                return cached
            index: dict[str, dict[str, Any]] = {}
            # Build once from the current corpus. Shipped legacy static hashes
            # cannot override a changed/removed native phrase.
            if key == "ru":
                for row in self._curated_rows():
                    if row.get("intent") not in _EXACT_HASH_ALLOWLIST:
                        continue
                    phrase = str(row.get("phrase", "") or "")
                    if phrase and phrase not in index:
                        index[phrase] = row
            # Native exact phrases must be checked before fuzzy matching, and
            # newly shipped phrases cannot depend on stale precomputed hashes.
            owners: dict[str, set[str]] = {}
            rows_by_phrase: dict[str, dict[str, Any]] = {}
            data, commands = self._load_language_generation(key)
            for row in self._seed_rows(key, data=data):
                phrase = str(row.get("phrase", ""))
                owners.setdefault(phrase, set()).add(str(row.get("intent", "")))
                if row.get("intent") in _EXACT_HASH_ALLOWLIST:
                    rows_by_phrase[phrase] = row
            for phrase, row in rows_by_phrase.items():
                if len(owners.get(phrase, ())) == 1:
                    index[phrase] = row
                else:
                    index.pop(phrase, None)
            generation = (commands,) if commands is not None else ()
            ready = generation if generation and self._generation_is_current(generation) else None
            self._bounded_locale_cache_put(self._exact_cache, key, index)
            self._bounded_locale_cache_put(self._exact_generations, key, ready)
            return index

    def context(self) -> AtlasContext:
        if self.memory is None:
            return AtlasContext()
        active_process = active_window = active_folder = ""
        game_id = game_name = ""
        try:
            sys_state = self.memory.system_snapshot()
            current = sys_state.get("current_window") if isinstance(sys_state, dict) else {}
            if isinstance(current, dict):
                active_process = str(current.get("process", "") or "")
                active_window = str(current.get("title", "") or "")
                active_folder = str(current.get("folder", "") or "")
        except Exception:
            pass
        try:
            state = self.memory.state_snapshot()
            last_game = str(state.get("last_game_name", "") or "") if isinstance(state, dict) else ""
            game_state = self.memory.game_snapshot(last_game) if last_game else {}
            current_session = game_state.get("current_session") if isinstance(game_state, dict) else {}
            if isinstance(current_session, dict) and current_session:
                status = str(current_session.get("status", "running") or "running").casefold()
                if status == "running":
                    game_id = str(current_session.get("game_id", "") or game_state.get("game_id", "") or "")
                    game_name = str(current_session.get("game_name", "") or game_state.get("game_name", "") or last_game)
        except Exception:
            pass
        domain = "gaming" if game_id or game_name else "system"
        return AtlasContext(
            domain=domain, active_game_id=game_id, active_game_name=game_name,
            active_process=active_process, active_window=active_window,
            active_folder=active_folder,
        )

    def exact_hash_match(self, text: str, *, language: str = "ru") -> AtlasIntent | None:
        if not self.enabled() or not bool(self._cfg("routing.static_hash_enabled", True)):
            return None
        row = self._exact_index(language).get(_norm(text))
        if not row:
            return None
        return AtlasIntent(
            intent=str(row["intent"]), tool=str(row["tool"]),
            arguments=dict(row.get("arguments") or {}), domain=str(row.get("domain") or "general"),
            confidence=1.0, priority=930, source="static.hash", executable=True,
            negative_rules=("atlas2 exact hash",), matched_phrase=str(row.get("phrase") or ""),
        )

    def semantic_match(self, text: str, *, language: str = "ru") -> AtlasIntent | None:
        if not self.semantic_enabled():
            return None
        normalized = _norm(text)
        if not normalized or len(normalized) < 3:
            return None
        rows, matrix = self._semantic_index(language)
        if matrix.size == 0:
            return None
        query = self._vectorizer.encode_one(normalized)
        if not np.any(query):
            return None
        scores = matrix @ query
        if not len(scores):
            return None
        # Gather the best phrase, but also require a margin over a competing intent.
        count = min(12, len(scores))
        candidates = np.argpartition(scores, -count)[-count:]
        order = candidates[np.argsort(scores[candidates])[::-1]]
        best_idx = int(order[0])
        best_score = float(scores[best_idx])
        best = rows[best_idx]
        second_intent_score = 0.0
        for idx in order[1:]:
            row = rows[int(idx)]
            if str(row.get("intent")) != str(best.get("intent")):
                second_intent_score = float(scores[int(idx)])
                break
        threshold = float(self._cfg("routing.semantic_threshold", 0.43) or 0.43)
        # Very short commands are naturally less distinctive; demand more evidence.
        if len(_tokens(normalized)) <= 2:
            threshold = max(threshold, 0.55)
        margin = float(self._cfg("routing.semantic_margin", 0.05) or 0.05)
        if best_score < threshold or (second_intent_score and best_score - second_intent_score < margin):
            return None

        executable = bool(best.get("executable", False))
        # Negated requests may be discussed by the AI, never auto-executed from
        # semantic similarity ("не хочу выключать компьютер" is the canonical case).
        if _NEGATION_RE.search(" " + normalized + " "):
            executable = False

        seed_tokens = set(_tokens(str(best.get("phrase", ""))))
        input_tokens = set(_tokens(normalized))
        # Function words shared by Romance languages must not authorize an
        # unrelated desktop operation. In particular Portuguese "O que é
        # entropia?" used to execute briefing.summary through hashed overlap.
        # Exact/native grammars run first; a semantic execution also needs one
        # substantive lexical anchor from the selected candidate.
        grammatical = set("a an the this that of to for in on is are was be what who how which me my show tell please current de la le les un une des du est sont que qui quel quelle montre affiche moi mon o os as um uma é são e do da dos das qual como mostra meu el los las una es son qué quién cuál muestra mi il lo gli i uno è sono cosa chi quale mostra mio der die das ein eine ist sind was wer wie welcher zeige mir mein и а в на из для что кто как какой какая это есть покажи скажи мне мой пожалуйста текущий сейчас".split())
        # NEO 175: pronouns and particles are no anchor either («whats up» matched «bring up task
        # manager» through «up», «you're awesome» matched «what have you remembered» through «you»).
        grammatical |= _NOT_ANCHORS175
        anchors = (seed_tokens & input_tokens) - grammatical
        if executable and not anchors:
            executable = False
        # A weak similarity needs two shared meaningful words.
        if executable and best_score < 0.55 and len(anchors) < 2:
            executable = False
        extras = {x for x in input_tokens - seed_tokens if x not in _FILLER_WORDS}
        # Read-only/no-payload status intents can tolerate natural descriptive
        # wording. Target-bearing window commands stay conservative: a semantic
        # match may classify them, but named targets must still be parsed by the
        # deterministic router/AI tool-selection path.
        payload_sensitive = str(best.get("intent") or "").startswith("window.")
        if executable and payload_sensitive and len(extras) > 1:
            executable = False
        # Generic media words such as "пауза" are ownership-sensitive in NEO
        # JARVIS (Discord/Yandex/YouTube). Semantic similarity may classify them,
        # but execution waits for the existing real-playback-owner resolver unless
        # the service/video object was explicitly named.
        if executable and str(best.get("domain")) == "media":
            explicit_media = bool(re.search(
                r"\b(?:discord|дискорд\w*|музыкальн\w*\s+бот\w*|dj\s*bot|яндекс\w*|yandex(?:\s+music)?|youtube|ютуб\w*|ютьюб\w*|видео\w*|ролик\w*)\b",
                normalized, re.I,
            ))
            if not explicit_media:
                executable = False

        return AtlasIntent(
            intent=str(best.get("intent") or ""), tool=str(best.get("tool") or ""),
            arguments=dict(best.get("arguments") or {}), domain=str(best.get("domain") or "general"),
            confidence=best_score, priority=710, source="semantic.vector",
            executable=executable,
            negative_rules=(f"semantic margin={best_score-second_intent_score:.3f}", "no destructive semantic execution"),
            matched_phrase=str(best.get("phrase") or ""),
        )




    def resolve(
        self,
        text: str,
        *,
        router: Any,
        language: str = "ru",
        semantic_text: str | None = None,
        scenario_phrases: list[str] | None = None,
    ) -> AtlasResolution:
        """Resolve a single command through Atlas 2.0 without invoking an LLM."""
        if not self.enabled():
            routed = router.route(text, scenario_phrases=scenario_phrases)
            return AtlasResolution(routed=routed, source="legacy.static" if routed else "fallback")

        # Level 1A is intentionally memory-free: the hot deterministic path must
        # not perform disk IO or game-process inspection.
        routed = router.route(text, scenario_phrases=scenario_phrases)
        if routed is not None:
            return AtlasResolution(routed=routed, source="static.regex")

        exact = self.exact_hash_match(text, language=language)
        if exact is None and semantic_text and semantic_text != text:
            exact = self.exact_hash_match(semantic_text, language=language)
        if exact is not None:
            return AtlasResolution(routed=exact, semantic=exact, source="static.hash")

        # Context is evaluated lazily only after the static layers miss.
        ctx = self.context()

        semantic_input = str(semantic_text or text or "")
        semantic = self.semantic_match(semantic_input, language=language)
        # HOTFIX105 strict isolation: never consult another language matrix after
        # a miss.  The deterministic router may use an internal canonical surface,
        # but semantic acceptance is always based on the selected locale only.
        if semantic is not None:
            return AtlasResolution(
                routed=semantic if semantic.executable else None, semantic=semantic,
                context=ctx, source=semantic.source,
            )
        return AtlasResolution(context=ctx, source="fallback")

    @staticmethod
    def prompt_hint(intent: AtlasIntent | None, context: AtlasContext) -> str:
        bits: list[str] = []
        if intent is not None:
            bits.append(
                f"Atlas2 candidate: domain={intent.domain}; intent={intent.intent}; "
                f"tool={intent.tool}; confidence={intent.confidence:.3f}; source={intent.source}."
            )
        if context.active_game_id or context.active_game_name:
            bits.append(f"Active game context: {context.active_game_name or context.active_game_id} ({context.active_game_id or 'generic'}).")
        if context.active_process or context.active_window:
            bits.append(f"System context: process={context.active_process or '?'}; window={context.active_window or '?' }.")
        if context.active_folder:
            bits.append(f"Explorer context: folder={context.active_folder}.")
        return " ".join(bits)[:1800]
