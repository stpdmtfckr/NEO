# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Low-cost lifelong ambient learning for NEO JARVIS.

The subsystem intentionally does not fine-tune Qwen and never grants execution
rights.  It learns a compact local model from speech/system observations and
retrieves only relevant memories for later turns.  Microphone learning reuses the
wake recognizer's already-produced transcript, so it adds no second microphone
STT pass.  System-audio speech is optional/best-effort and only runs on a true WASAPI loopback input while the resource governor says the PC is idle enough.
Raw PCM is never persisted.
"""

import base64
import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from jarvis.runtime.atlas2 import CompactSemanticVectorizer
from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.runtime.audio_provenance import compact_audio_envelope, best_envelope_similarity
from jarvis.runtime.resource_budget import resource_budget
from jarvis.runtime.distribution164 import worker_command
from jarvis.voice import RecordingResult, TranscriptionService

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[0-9a-zа-яё][0-9a-zа-яё_+.#-]{1,47}", re.I)
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}", re.I),
    re.compile(r"(?i)(\b(?:парол(?:ь|я)|password|token|secret|api\s*key)\s*[:=]\s*)([^\s,;]+)"),
)
_STOPWORDS = {
    # RU
    "это", "этот", "эта", "эти", "того", "тому", "там", "тут", "вот", "как", "что", "чтобы", "когда",
    "где", "который", "которая", "которые", "или", "если", "тоже", "уже", "ещё", "еще", "просто", "очень",
    "типа", "короче", "какбы", "как", "бы", "ну", "да", "нет", "ага", "угу", "блин", "сейчас", "потом",
    "меня", "мне", "мой", "моя", "моё", "мои", "тебя", "тебе", "твой", "его", "её", "их", "мы", "вы",
    "они", "она", "оно", "он", "я", "ты", "же", "ли", "не", "ни", "на", "в", "во", "к", "ко", "с", "со",
    "из", "за", "по", "для", "про", "от", "до", "при", "над", "под", "без", "у", "и", "а", "но", "то",
    # EN/common multilingual filler
    "the", "this", "that", "these", "those", "and", "or", "but", "with", "from", "for", "into", "about",
    "what", "when", "where", "which", "who", "how", "why", "just", "really", "very", "like", "yeah", "yes",
    "no", "okay", "ok", "well", "then", "now", "later", "you", "your", "me", "my", "we", "our", "they",
    "he", "she", "it", "is", "are", "was", "were", "be", "been", "to", "of", "in", "on", "at", "as", "a", "an",
    "application", "window", "folder", "system", "audio", "active", "process", "context",
}
_STYLE_MARKERS = (
    "короче", "типа", "давай", "ладно", "окей", "понял", "прикол", "блин", "ну", "вот",
    "okay", "ok", "well", "basically", "actually", "let's", "cool", "right",
)
_MUSIC_ONLY_PROCESS_HINTS = {
    "spotify", "yandexmusic", "yandex_music", "music", "aimp", "foobar2000", "winamp"
}

_USER_SPEECH_SOURCES = frozenset({"microphone", "user_monologue", "conversation"})
_SYSTEM_OBSERVATION_SOURCES = frozenset({"system_audio", "system_audio_activity", "system_context", "loopback", "wasapi_loopback"})
_UNTRUSTED_COMMAND_TOKENS = frozenset({
    "jarvis", "джарвис", "жарвис", "open", "открой", "открыть", "close", "закрой", "закрыть",
    "delete", "remove", "удали", "удалить", "run", "execute", "запусти", "запустить", "shutdown",
    "выключи", "restart", "перезагрузи", "send", "отправь", "write", "напиши", "click", "нажми",
    "ignore", "instructions", "instruction", "инструкции", "команда", "command", "tool", "tools",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, limit: int = 1600) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _redact(text: Any) -> str:
    value = _clip(text)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            value = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "<redacted>", value)
        else:
            value = pattern.sub("<redacted>", value)
    return value


def _norm(text: Any) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я_+.#-]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN_RE.findall(str(text or "")):
        token = raw.casefold().replace("ё", "е").strip("._-+")
        if len(token) < 3 or token in _STOPWORDS or token.isdigit():
            continue
        if token not in out:
            out.append(token)
    return out[:48]


def _display_terms(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in _TOKEN_RE.findall(str(text or "")):
        key = _norm(raw)
        if key and len(key) >= 3 and key not in _STOPWORDS and not key.isdigit():
            result.setdefault(key, raw)
    return result


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        return "{}"


@dataclass(slots=True)
class ResourceDecision:
    allow: bool
    reason: str = ""
    cpu_percent: float = 0.0


class AmbientResourceGovernor:
    """Cheap cached resource gate. Background learning must always yield first."""

    def __init__(self, config: Any, busy_provider: Callable[[], bool] | None = None) -> None:
        self.config = config
        self.busy_provider = busy_provider
        self._last_probe = 0.0
        self._last_cpu = 0.0
        self._lock = threading.Lock()

    def _get(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _cpu(self) -> float:
        now = time.monotonic()
        with self._lock:
            if now - self._last_probe < 2.5:
                return self._last_cpu
            self._last_probe = now
            try:
                import psutil
                self._last_cpu = float(psutil.cpu_percent(interval=None) or 0.0)
            except Exception:
                self._last_cpu = 0.0
            return self._last_cpu

    @staticmethod
    def _game_active() -> bool:
        try:
            from jarvis.gaming.runtime import active_game_context
            ctx = active_game_context(max_age_seconds=12.0) or {}
            return bool(ctx.get("game_name") or ctx.get("game_id"))
        except Exception:
            return False

    def allow_heavy(self) -> ResourceDecision:
        if not bool(self._get("ambient_learning.enabled", True)):
            return ResourceDecision(False, "disabled")
        if resource_budget().should_defer_heavy_background():
            return ResourceDecision(False, "foreground_or_game")
        if bool(self._get("ambient_learning.pause_while_assistant_busy", True)) and self.busy_provider is not None:
            try:
                if bool(self.busy_provider()):
                    return ResourceDecision(False, "assistant_busy")
            except Exception:
                pass
        if bool(self._get("ambient_learning.pause_during_game", True)) and self._game_active():
            return ResourceDecision(False, "active_game")
        cpu = self._cpu()
        threshold = max(20.0, min(90.0, float(self._get("ambient_learning.max_background_cpu_percent", 48.0) or 48.0)))
        if cpu >= threshold:
            return ResourceDecision(False, "cpu", cpu)
        return ResourceDecision(True, "", cpu)

    def allow_cheap(self) -> bool:
        return bool(self._get("ambient_learning.enabled", True))


class AmbientMemory:
    """Compact local episodic/topic memory backed by SQLite + 384D Atlas vectors."""

    def __init__(self, config: Any) -> None:
        self.config = config
        base = Path(getattr(getattr(config, "paths", None), "root", Path.home() / ".neo_jarvis"))
        self.root = base / "context_memory" / "ambient"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "ambient_memory.db"
        self._lock = threading.RLock()
        self._vectorizer = CompactSemanticVectorizer(int(self._get("routing.semantic_dimension", 384) or 384))
        self._cache_at = 0.0
        self._recent_cache: list[tuple[int, str, str, str, float, float, str, bytes]] = []
        self._term_cache_at = 0.0
        self._term_cache: list[dict[str, Any]] = []
        self._init_db()

    def _get(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=2.5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    text TEXT NOT NULL,
                    norm_text TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    importance REAL NOT NULL DEFAULT 0.5,
                    fingerprint TEXT NOT NULL UNIQUE,
                    vector BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ambient_obs_created ON observations(created_at DESC);
                CREATE TABLE IF NOT EXISTS terms (
                    term TEXT PRIMARY KEY,
                    display TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.2,
                    sources_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_ambient_terms_rank ON terms(count DESC, last_seen DESC);
                CREATE TABLE IF NOT EXISTS associations (
                    left_term TEXT NOT NULL,
                    right_term TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT NOT NULL,
                    PRIMARY KEY(left_term, right_term)
                );
                CREATE TABLE IF NOT EXISTS style_stats (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL DEFAULT 0
                );
                """
            )

    def enabled(self) -> bool:
        return bool(self._get("ambient_learning.enabled", True))

    @staticmethod
    def _fingerprint(source: str, text: str, context: dict[str, Any]) -> str:
        # One-minute bucket removes echo/repeated wake chunks without collapsing a
        # genuinely repeated phrase hours later (repetition itself is learning).
        bucket = int(time.time() // 60)
        useful_ctx = {
            "process": context.get("process"), "folder": context.get("folder"),
            "game": context.get("game"), "audio_process": context.get("audio_process"),
        }
        raw = f"{bucket}|{source}|{_norm(text)}|{_safe_json(useful_ctx)}"
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _importance(text: str, context: dict[str, Any], source: str) -> float:
        score = 0.38
        toks = _tokens(text)
        if len(toks) >= 4:
            score += 0.08
        if any(context.get(k) for k in ("folder", "game", "process", "window_title")):
            score += 0.10
        if source in _USER_SPEECH_SOURCES:
            score += 0.08
        if re.search(r"\b(?:проект|project|ошибк|problem|проблем|надо|нужно|план|идея|думаю|хочу|работа|game|игр)\w*\b", text, re.I):
            score += 0.10
        return max(0.1, min(0.95, score))

    def observe(
        self,
        text: str,
        *,
        source: str,
        context: dict[str, Any] | None = None,
        confidence: float = 0.55,
        importance: float | None = None,
    ) -> bool:
        if not self.enabled():
            return False
        clean = _redact(text)
        normalized = _norm(clean)
        if len(normalized) < max(4, int(self._get("ambient_learning.min_text_chars", 6) or 6)):
            return False
        if len(_tokens(clean)) == 0 and len(normalized.split()) < 2:
            return False
        context = dict(context or {})
        confidence = max(0.05, min(0.99, float(confidence)))
        importance = self._importance(clean, context, source) if importance is None else max(0.05, min(0.99, float(importance)))
        fingerprint = self._fingerprint(source, clean, context)
        vector = self._vectorizer.encode_one(clean).astype(np.float32, copy=False).tobytes()
        now = _utc_now()
        display = _display_terms(clean)
        terms = list(display)[:24]
        # Add local context names as associations without pretending the user said them.
        for raw in (context.get("process"), context.get("game"), context.get("folder_name")):
            token = _norm(raw)
            if token and 3 <= len(token) <= 48 and token not in terms:
                terms.append(token)
                display.setdefault(token, str(raw))
        with self._lock, closing(self._connect()) as conn, conn:
            try:
                conn.execute(
                    "INSERT INTO observations(created_at,source,text,norm_text,context_json,confidence,importance,fingerprint,vector) VALUES(?,?,?,?,?,?,?,?,?)",
                    (now, str(source)[:48], clean, normalized, _safe_json(context), confidence, importance, fingerprint, sqlite3.Binary(vector)),
                )
            except sqlite3.IntegrityError:
                return False
            for term in terms[:28]:
                if term in _STOPWORDS or len(term) < 3:
                    continue
                shown = _clip(display.get(term, term), 80)
                row = conn.execute("SELECT count, confidence, sources_json FROM terms WHERE term=?", (term,)).fetchone()
                if row:
                    count = int(row[0] or 0) + 1
                    old_conf = float(row[1] or 0.2)
                    try:
                        sources = json.loads(row[2] or "{}")
                    except Exception:
                        sources = {}
                    sources[str(source)] = int(sources.get(str(source), 0) or 0) + 1
                    new_conf = min(0.98, max(old_conf, 0.18 + min(0.70, count * 0.055)))
                    conn.execute(
                        "UPDATE terms SET display=?, count=?, last_seen=?, confidence=?, sources_json=? WHERE term=?",
                        (shown, count, now, new_conf, _safe_json(sources), term),
                    )
                else:
                    conn.execute(
                        "INSERT INTO terms(term,display,count,first_seen,last_seen,confidence,sources_json) VALUES(?,?,?,?,?,?,?)",
                        (term, shown, 1, now, now, 0.24, _safe_json({str(source): 1})),
                    )
            if source in {"microphone", "user_monologue", "conversation"}:
                word_count = len(re.findall(r"[0-9a-zа-яё]+", clean, flags=re.I))
                conn.execute(
                    "INSERT INTO style_stats(key,value) VALUES('utterances',1) ON CONFLICT(key) DO UPDATE SET value=value+1"
                )
                conn.execute(
                    "INSERT INTO style_stats(key,value) VALUES('words',?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
                    (float(word_count),),
                )
                low = clean.casefold()
                for marker in _STYLE_MARKERS:
                    hits = len(re.findall(r"(?<!\w)" + re.escape(marker.casefold()) + r"(?!\w)", low))
                    if hits:
                        conn.execute(
                            "INSERT INTO style_stats(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
                            ("marker:" + marker, float(hits)),
                        )

            # Bounded pairwise co-occurrence graph: enough to learn project/topic
            # associations without a graph database or background model.
            pair_terms = terms[:10]
            for i, left in enumerate(pair_terms):
                for right in pair_terms[i + 1:]:
                    if left == right:
                        continue
                    a, b = sorted((left, right))
                    conn.execute(
                        "INSERT INTO associations(left_term,right_term,count,last_seen) VALUES(?,?,1,?) "
                        "ON CONFLICT(left_term,right_term) DO UPDATE SET count=count+1,last_seen=excluded.last_seen",
                        (a, b, now),
                    )
        self._cache_at = 0.0
        self._term_cache_at = 0.0
        return True

    def learned_terms(self, *, limit: int = 80, min_count: int | None = None) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        now = time.monotonic()
        if self._term_cache and now - self._term_cache_at < 15.0:
            return self._term_cache[:limit]
        minimum = int(min_count if min_count is not None else self._get("ambient_learning.learned_term_min_count", 2) or 2)
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT term,display,count,last_seen,confidence,sources_json FROM terms WHERE count>=? ORDER BY confidence DESC,count DESC,last_seen DESC LIMIT 160",
                (max(1, minimum),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                sources_map = json.loads(r[5] or "{}")
            except Exception:
                sources_map = {}
            # Only speech actually captured from the user's microphone may teach
            # the command-side SpeechUnderstanding vocabulary. System loopback
            # remains knowledge/context and can never teach executable phrases.
            trusted_count = sum(int(sources_map.get(name, 0) or 0) for name in _USER_SPEECH_SOURCES)
            if trusted_count < max(1, minimum):
                continue
            out.append({
                "term": r[0], "display": r[1], "count": int(r[2]), "trusted_count": trusted_count,
                "last_seen": r[3], "confidence": float(r[4]), "sources": r[5],
            })
        self._term_cache = out
        self._term_cache_at = now
        return out[:limit]

    def _recent_rows(self, limit: int = 420) -> list[tuple[int, str, str, str, float, float, str, bytes]]:
        now = time.monotonic()
        if self._recent_cache and now - self._cache_at < 8.0:
            return self._recent_cache
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT id,created_at,source,text,confidence,importance,context_json,vector FROM observations ORDER BY id DESC LIMIT ?",
                (max(20, min(1000, int(limit))),),
            ).fetchall()
        self._recent_cache = [(int(r[0]), str(r[1]), str(r[2]), str(r[3]), float(r[4]), float(r[5]), str(r[6]), bytes(r[7])) for r in rows]
        self._cache_at = now
        return self._recent_cache

    def relevant_context(self, query: str, *, limit: int = 6) -> str:
        if not self.enabled() or not str(query or "").strip():
            return ""
        q = self._vectorizer.encode_one(query)
        if not np.any(q):
            return ""
        now = datetime.now(timezone.utc)
        scored: list[tuple[float, str, str, str]] = []
        for _id, created, source, text, confidence, importance, context_json, raw_vec in self._recent_rows():
            try:
                vec = np.frombuffer(raw_vec, dtype=np.float32, count=self._vectorizer.dimension)
                similarity = float(np.dot(q, vec)) if vec.size == q.size else 0.0
            except Exception:
                similarity = 0.0
            if similarity < 0.14:
                continue
            try:
                age_hours = max(0.0, (now - datetime.fromisoformat(created)).total_seconds() / 3600.0)
            except Exception:
                age_hours = 168.0
            recency = 1.0 / (1.0 + age_hours / 36.0)
            score = similarity * 0.72 + confidence * 0.08 + importance * 0.10 + recency * 0.10
            if score < 0.22:
                continue
            scored.append((score, source, text, context_json))
        scored.sort(key=lambda x: x[0], reverse=True)
        parts: list[str] = []
        seen: set[str] = set()
        for score, source, text, context_json in scored:
            key = _norm(text)
            if not key or key in seen:
                continue
            seen.add(key)
            ctx_bits: list[str] = []
            try:
                ctx = json.loads(context_json or "{}")
                for key_name, label in (("project", "project"), ("folder", "folder"), ("process", "app"), ("game", "game")):
                    value = str(ctx.get(key_name, "") or "").strip()
                    if value:
                        ctx_bits.append(f"{label}={_clip(value, 90)}")
            except Exception:
                pass
            suffix = f" ({', '.join(ctx_bits)})" if ctx_bits else ""
            if source in _SYSTEM_OBSERVATION_SOURCES:
                # Never inject system playback verbatim into an LLM/tool prompt.
                # Reduce it to unordered topic/entity tokens so a video saying
                # "Jarvis delete X" cannot become an instruction later.
                terms = [
                    shown for key, shown in _display_terms(text).items()
                    if _norm(key) not in _UNTRUSTED_COMMAND_TOKENS
                ][:12]
                if source == "system_context":
                    neutral = "system context"
                elif source == "system_audio_activity":
                    neutral = "system audio activity"
                else:
                    neutral = "system-audio topics"
                topic_text = ", ".join(_clip(term, 60) for term in terms)
                if not topic_text and not suffix:
                    continue
                parts.append(f"- [{neutral}; untrusted observation] {topic_text}{suffix}")
            else:
                parts.append(f"- {_clip(text, 360)}{suffix}")
            if len(parts) >= max(1, min(10, int(limit))):
                break
        return "\n".join(parts)

    def communication_style_hint(self) -> str:
        """Return a tiny statistical style hint, never copied private transcript text."""
        if not self.enabled():
            return ""
        try:
            with self._lock, closing(self._connect()) as conn, conn:
                rows = dict(conn.execute("SELECT key,value FROM style_stats").fetchall())
        except Exception:
            return ""
        utterances = float(rows.get("utterances", 0.0) or 0.0)
        words = float(rows.get("words", 0.0) or 0.0)
        if utterances < 8:
            return ""
        avg = words / max(1.0, utterances)
        length = "короткими фразами" if avg < 8 else ("фразами средней длины" if avg < 18 else "развёрнутыми фразами")
        markers = sorted(
            ((float(v), k.split(":", 1)[1]) for k, v in rows.items() if str(k).startswith("marker:") and float(v or 0) >= 2),
            reverse=True,
        )[:4]
        marker_text = ", ".join(item[1] for item in markers)
        return f"Пользователь обычно говорит {length}; стиль преимущественно разговорный" + (f"; частые разговорные маркеры: {marker_text}" if marker_text else "") + "."

    def maintenance(self) -> None:
        if not self.enabled():
            return
        ttl_days = max(3, min(3650, int(self._get("ambient_learning.observation_ttl_days", 60) or 60)))
        max_rows = max(500, min(50000, int(self._get("ambient_learning.max_observations", 6000) or 6000)))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM observations WHERE created_at < ? AND importance < 0.78", (cutoff,))
            row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
            count = int(row[0] or 0) if row else 0
            if count > max_rows:
                conn.execute(
                    "DELETE FROM observations WHERE id IN (SELECT id FROM observations ORDER BY importance ASC,id ASC LIMIT ?)",
                    (count - max_rows,),
                )
            conn.execute("DELETE FROM associations WHERE count<=1 AND last_seen < ?", (cutoff,))
        self._cache_at = 0.0


class SystemAudioSpeechMonitor:
    """Best-effort *true WASAPI loopback* speech monitor.

    HOTFIX124 deliberately forbids Stereo-Mix/general recording endpoints here.
    System playback is captured only by ``ambient_loopback_worker`` from a
    SoundCard microphone explicitly marked ``isloopback``. The user's physical
    microphone remains owned exclusively by AudioCaptureController/wake/command.
    """

    def __init__(self, config: Any, controller: "AmbientLearningController") -> None:
        self.config = config
        self.controller = controller
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._process_generation = 0
        self._stt: TranscriptionService | None = None
        self._last_stt_use = 0.0
        self._last_accept = 0.0
        self._decode_retry_at = 0.0
        self._decode_failures = 0
        self._chunks: queue.Queue[tuple[float, RecordingResult]] = queue.Queue(maxsize=1)
        self._decode_thread: threading.Thread | None = None

    def _get(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[2]

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        root = self._project_root()
        old = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(root) + (os.pathsep + old if old else "")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("MKL_NUM_THREADS", "1")
        return env

    def start(self) -> None:
        if os.name != "nt" or not (self._transcription_enabled() or bool(self._get("ambient_learning.system_echo_guard_enabled", True))):
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="NeoJarvis-AmbientSystemAudio", daemon=True)
        self._thread.start()

    def _transcription_enabled(self) -> bool:
        return bool(self._get("ambient_learning.enabled", True)) and bool(self._get("ambient_learning.system_audio_transcription_enabled", False))

    def stop(self) -> None:
        self._stop.set()
        if self._stt is not None:
            self._stt.cancel()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                if process.stdin:
                    process.stdin.write('{"cmd":"shutdown"}\n'); process.stdin.flush()
            except Exception:
                pass
            try:
                process.terminate()
            except Exception:
                pass
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._decode_thread and self._decode_thread.is_alive() and self._decode_thread is not threading.current_thread():
            self._decode_thread.join(timeout=2.0)
        self._decode_thread = None

    def _close_stt(self) -> None:
        service, self._stt = self._stt, None
        if service is not None:
            try:
                service.close()
            except Exception:
                pass

    def _ensure_stt(self) -> TranscriptionService:
        if self._stt is None:
            self._stt = TranscriptionService("NeoJarvis-STT-AmbientSystem", process_priority="below_normal", background_cpu_cores=.25)
        return self._stt

    def _start_soundcard_worker(self) -> subprocess.Popen[str]:
        settings = {
            "transcription_enabled": self._transcription_enabled(),
            "echo_enabled": bool(self._get("ambient_learning.system_echo_guard_enabled", True)),
            "device": self._get("ambient_learning.system_audio_device", "auto"),
            "silence_seconds": self._get("ambient_learning.system_audio_silence_seconds", 0.85),
            "max_seconds": self._get("ambient_learning.system_audio_chunk_seconds", 8.0),
            "min_seconds": self._get("ambient_learning.system_audio_min_speech_seconds", 1.2),
        }
        process = subprocess.Popen(
            worker_command("jarvis.ambient_loopback_worker", args=(json.dumps(settings),)),
            cwd=str(self._project_root()), env=self._env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            **hidden_process_kwargs(),
        )
        from jarvis.runtime.process_diagnostics184 import record_spawn
        self._process_generation += 1
        record_spawn("system_loopback", process.pid, self._process_generation,
                     configured_echo_enabled=settings["echo_enabled"],
                     configured_system_transcription_enabled=settings["transcription_enabled"])
        try:
            import psutil
            if os.name == "nt":
                psutil.Process(process.pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
        return process

    def _transcribe(self, recording: RecordingResult) -> None:
        if self._stop.is_set() or not self._transcription_enabled():
            return
        duration = float(recording.audio.size) / max(1, int(recording.sample_rate))
        if duration < float(self._get("ambient_learning.system_audio_min_speech_seconds", 1.2) or 1.2):
            return
        # Dedicated music players are poor speech sources and Whisper may invent
        # lyrics/titles from music. Browser/Discord/video apps remain eligible.
        active = str(getattr(self.controller, "_last_system_audio_process", "") or "").casefold()
        active_parts = {re.sub(r"[^a-z0-9_]+", "", x) for x in active.split(",") if x.strip()}
        if active_parts and all(any(h in part for h in _MUSIC_ONLY_PROCESS_HINTS) for part in active_parts):
            return
        gap = max(2.0, float(self._get("ambient_learning.system_audio_min_stt_gap_seconds", 6.0) or 6.0))
        now = time.monotonic()
        if now < self._decode_retry_at or now - self._last_accept < gap:
            return
        decision = self.controller.governor.allow_heavy()
        if not decision.allow:
            return
        self._last_accept = now
        service = self._ensure_stt()
        if self._stop.is_set():
            service.cancel()
            return
        self._last_stt_use = now
        try:
            text = service.transcribe(
                recording,
                model_name=str(self._get("ambient_learning.stt_model", "tiny") or "tiny"),
                language="auto",
                device="cpu",
                compute_type="int8",
                cpu_threads=max(1, min(2, int(self._get("ambient_learning.stt_cpu_threads", 1) or 1))),
                timeout_seconds=35.0,
                purpose="ambient",
                initial_prompt="",
                hotwords="",
                noise_rejection_percent=55,
                adaptive_decode_retry=False,
            ).strip()
        except Exception:
            self._decode_failures += 1
            # A timed-out native decoder is killed by TranscriptionService.
            # Do not immediately reload it for the very next audio chunk.
            self._decode_retry_at = time.monotonic() + min(300.0, 30.0 * 2 ** min(4, self._decode_failures - 1))
            logger.debug("Ambient system-audio STT failed", exc_info=True)
            return
        self._decode_failures = 0
        self._decode_retry_at = 0.0
        if text:
            self.controller.observe_system_audio_transcript(text)

    def _decode_loop(self) -> None:
        """One disposable chunk at a time; never block the echo-reference reader."""
        try:
            while not self._stop.is_set():
                try:
                    queued_at, recording = self._chunks.get(timeout=1.0)
                except queue.Empty:
                    if self._stt and time.monotonic() - self._last_stt_use > float(self._get("ambient_learning.stt_idle_unload_seconds", 120.0) or 120.0):
                        self._close_stt()
                    continue
                if time.monotonic() - queued_at <= 5.0:
                    self._transcribe(recording)
        finally:
            self._close_stt()

    def _run(self) -> None:
        # Delay startup so command/wake workers and UI win cold-start resources.
        if self._stop.wait(max(0.20, float(self._get("ambient_learning.system_audio_start_delay_seconds", 0.75) or 0.75))):
            return
        if self._transcription_enabled():
            self._decode_thread = threading.Thread(target=self._decode_loop, name="NEO-AmbientDecode", daemon=True)
            self._decode_thread.start()
        failures = 0
        while not self._stop.is_set():
            process_started_at = time.monotonic()
            try:
                # Only an explicitly marked WASAPI loopback endpoint is allowed.
                self._process = self._start_soundcard_worker()
                if self._stop.is_set():
                    break
                logger.info("System echo capture started; ambient transcription=%s", self._transcription_enabled())
                stream = self._process.stdout
                if stream is None:
                    raise RuntimeError("Loopback worker stdout unavailable")
                for line in stream:
                    if self._stop.is_set():
                        break
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("type", "") or "")
                    if event_type == "echo_reference":
                        if str(event.get("provenance", "") or "") == "wasapi_loopback":
                            self.controller.observe_system_audio_reference(event.get("signature"))
                        continue
                    if event_type != "ambient_chunk" or not self._transcription_enabled():
                        continue
                    encoded = str(event.get("audio_b64", "") or "")
                    if not encoded:
                        continue
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                        audio = np.frombuffer(raw, dtype=np.float32).copy()
                    except Exception:
                        continue
                    item = (time.monotonic(), RecordingResult(audio, int(event.get("sample_rate", 16000) or 16000)))
                    try:
                        self._chunks.put_nowait(item)
                    except queue.Full:
                        try:
                            self._chunks.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._chunks.put_nowait(item)
                        except queue.Full:
                            pass
            except Exception:
                logger.debug("Ambient system-audio monitor stopped after error", exc_info=True)
            finally:
                process, self._process = self._process, None
                if process is not None:
                    try:
                        process.terminate()
                        process.wait(timeout=1.5)
                    except Exception:
                        pass
            if time.monotonic() - process_started_at >= 30.0:
                failures = 0
            failures += 1
            if self._stop.wait(min(60.0, 2.0 ** min(failures, 6))):
                break


class AmbientLearningController:
    """Coordinates lifelong learning without entering the command execution path."""

    def __init__(
        self,
        config: Any,
        *,
        context_memory: Any = None,
        busy_provider: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        self.context_memory = context_memory
        self.memory = AmbientMemory(config)
        self.governor = AmbientResourceGovernor(config, busy_provider)
        self._stop = threading.Event()
        self._queue: queue.Queue[tuple[str, str, dict[str, Any], float]] = queue.Queue(maxsize=96)
        self._worker: threading.Thread | None = None
        self._maintenance_at = 0.0
        self._audio = SystemAudioSpeechMonitor(config, self)
        self._last_window_context: dict[str, Any] = {}
        self._last_system_audio_process = ""
        self._audio_session_thread: threading.Thread | None = None
        self._echo_lock = threading.Lock()
        self._system_echo_refs: deque[tuple[float, np.ndarray]] = deque(maxlen=24)

    def _get(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def enabled(self) -> bool:
        return bool(self._get("ambient_learning.enabled", True))

    def start(self) -> None:
        # Echo protection is a provenance boundary, independent of learning.
        self._audio.start()
        if not self.enabled() or self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="NeoJarvis-AmbientLearning", daemon=True)
        self._worker.start()
        if bool(self._get("ambient_learning.system_audio_activity_enabled", True)) and os.name == "nt":
            self._audio_session_thread = threading.Thread(target=self._audio_sessions_loop, name="NeoJarvis-AudioContext", daemon=True)
            self._audio_session_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._audio.stop()
        if self._worker and self._worker.is_alive() and self._worker is not threading.current_thread():
            self._worker.join(timeout=2.0)
        if self._audio_session_thread and self._audio_session_thread.is_alive() and self._audio_session_thread is not threading.current_thread():
            self._audio_session_thread.join(timeout=1.5)
        self._worker = None
        self._audio_session_thread = None

    def _context(self) -> dict[str, Any]:
        ctx = dict(self._last_window_context)
        try:
            if self.context_memory is not None:
                snap = self.context_memory.system_snapshot()
                current = snap.get("current_window", {}) if isinstance(snap, dict) else {}
                if isinstance(current, dict):
                    ctx.setdefault("process", str(current.get("process", "") or ""))
                    ctx.setdefault("window_title", str(current.get("title", "") or ""))
                    folder = str(current.get("folder", "") or "")
                    if folder:
                        ctx.setdefault("folder", folder)
                        ctx.setdefault("folder_name", Path(folder).name)
        except Exception:
            pass
        try:
            from jarvis.gaming.runtime import active_game_context
            game = active_game_context(max_age_seconds=60.0) or {}
            name = str(game.get("game_name", "") or "")
            if name:
                ctx["game"] = name
        except Exception:
            pass
        if self._last_system_audio_process:
            ctx["audio_process"] = self._last_system_audio_process
        folder = str(ctx.get("folder", "") or "")
        if folder and not ctx.get("project"):
            name = Path(folder).name
            if name and name.casefold() not in {"desktop", "downloads", "documents", "рабочий стол", "загрузки", "документы"}:
                ctx["project"] = name
        return ctx

    def observe_system_audio_reference(self, signature: Any) -> None:
        """Store a tiny loopback fingerprint used only to reject speaker echo.

        The fingerprint contains no recoverable raw PCM and never enters memory.
        """
        if not bool(self._get("ambient_learning.system_echo_guard_enabled", True)):
            return
        try:
            vec = np.asarray(signature, dtype=np.float32).reshape(-1)
        except Exception:
            return
        if vec.size != 48 or not np.all(np.isfinite(vec)) or not np.any(vec):
            return
        norm = float(np.linalg.norm(vec))
        if norm <= 1e-6:
            return
        vec = (vec / norm).astype(np.float32, copy=False)
        with self._echo_lock:
            self._system_echo_refs.append((time.monotonic(), vec.copy()))

    def probable_system_echo(self, recording: RecordingResult | None) -> tuple[bool, float]:
        """Return whether a microphone wake candidate matches recent PC playback."""
        if recording is None or not bool(self._get("ambient_learning.system_echo_guard_enabled", True)):
            return False, 0.0
        try:
            mic_sig = compact_audio_envelope(recording.audio, int(recording.sample_rate))
        except Exception:
            return False, 0.0
        if not np.any(mic_sig):
            return False, 0.0
        now = time.monotonic()
        horizon = max(0.55, min(3.0, float(self._get("ambient_learning.system_echo_reference_horizon_seconds", 1.65) or 1.65)))
        with self._echo_lock:
            refs = [vec.copy() for ts, vec in self._system_echo_refs if now - ts <= horizon]
        if not refs:
            return False, 0.0
        best = 0.0
        for ref in refs:
            best = max(best, best_envelope_similarity(mic_sig, ref, max_shift=12, min_overlap=22))
        threshold = max(0.68, min(0.98, float(self._get("ambient_learning.system_echo_similarity_threshold", 0.84) or 0.84)))
        return best >= threshold, best

    def observe_system_audio_transcript(self, text: str) -> None:
        # Dedicated non-executable provenance.  System playback can enrich topics
        # and entities but can never train command phrases or become a user turn.
        self.observe_text(text, source="system_audio", confidence=0.48)

    def observe_text(self, text: str, *, source: str = "microphone", confidence: float = 0.55) -> None:
        if not self.enabled() or not str(text or "").strip():
            return
        # Cheap ingestion may continue under load because the expensive ASR already
        # happened for wake detection. Queueing + SQLite writes are tiny and bounded.
        item = (_clip(text, 1600), str(source)[:48], self._context(), float(confidence))
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Lifelong learning is explicitly lossy under pressure; command latency wins.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except Exception:
                pass

    def observe_microphone_transcript(self, text: str) -> None:
        if bool(self._get("ambient_learning.microphone_enabled", True)):
            self.observe_text(text, source="microphone", confidence=0.58)

    def observe_system(self, payload: dict[str, Any]) -> None:
        if not self.enabled() or not bool(self._get("ambient_learning.system_context_enabled", True)):
            return
        if not isinstance(payload, dict):
            return
        self._last_window_context = {
            "process": str(payload.get("process_name", "") or ""),
            "window_title": str(payload.get("title", "") or ""),
            "folder": str(payload.get("folder_path", "") or ""),
        }
        folder = self._last_window_context.get("folder", "")
        if folder:
            self._last_window_context["folder_name"] = Path(folder).name
        # Learn only useful human-readable metadata changes, not PIDs/HWNDs.
        bits = []
        process = self._last_window_context.get("process", "")
        title = self._last_window_context.get("window_title", "")
        if process:
            bits.append(f"application {process}")
        if title and title.casefold() not in str(process).casefold():
            bits.append(f"window {title}")
        if folder:
            bits.append(f"folder {folder}")
        if bits:
            self.observe_text("; ".join(bits), source="system_context", confidence=0.62)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text, source, context, confidence = self._queue.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - self._maintenance_at > 1800 and self.governor.allow_heavy().allow:
                    try:
                        self.memory.maintenance()
                    except Exception:
                        logger.debug("Ambient maintenance failed", exc_info=True)
                    self._maintenance_at = time.monotonic()
                continue
            try:
                self.memory.observe(text, source=source, context=context, confidence=confidence)
            except Exception:
                logger.debug("Ambient observation failed", exc_info=True)

    def _audio_sessions_loop(self) -> None:
        interval = max(2.5, min(20.0, float(self._get("ambient_learning.system_audio_activity_poll_seconds", 5.0) or 5.0)))
        last: set[str] = set()
        com_initialized = False
        try:
            try:
                import comtypes
                comtypes.CoInitialize()
                com_initialized = True
            except Exception:
                pass
            while not self._stop.wait(interval):
                if not self.governor.allow_cheap():
                    continue
                active: set[str] = set()
                try:
                    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
                    for session in AudioUtilities.GetAllSessions():
                        proc = getattr(session, "Process", None)
                        name = str(proc.name() if proc is not None else "").strip()
                        if not name or name.casefold() in {"audiodg.exe", "system"}:
                            continue
                        peak = 0.0
                        try:
                            ctl = session._ctl.QueryInterface(IAudioMeterInformation)  # type: ignore[attr-defined]
                            peak = float(ctl.GetPeakValue() or 0.0)
                        except Exception:
                            # Some pycaw versions do not expose a per-session meter;
                            # session presence is still useful but lower-confidence.
                            peak = 0.02
                        if peak >= 0.008:
                            active.add(Path(name).stem)
                except Exception:
                    logger.debug("Ambient audio-session context probe failed", exc_info=True)
                    continue
                if active == last:
                    continue
                last = active
                self._last_system_audio_process = ", ".join(sorted(active))[:180]
                if active:
                    self.observe_text(
                        "system audio active in " + ", ".join(sorted(active)),
                        source="system_audio_activity", confidence=0.45,
                    )
        finally:
            if com_initialized:
                try:
                    import comtypes
                    comtypes.CoUninitialize()
                except Exception:
                    pass
