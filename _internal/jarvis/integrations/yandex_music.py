# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import difflib
import json
import inspect
import logging
import os
import random
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any
import urllib.request

from jarvis.music_app_control import (
    control_music_app,
    focus_yandex_music_window,
    launch_yandex_music,
    music_app_status,
    normalize,
    play_track_in_yandex,
    play_my_tracks_in_yandex,
)
from jarvis.secrets import yandex_music_token
from jarvis.integrations.background.local_player import local_player
from jarvis.integrations.youtube_audio import YouTubeAudioResolver, normalize_youtube_url
from jarvis.runtime.wave_history import history_lock, merge_entries, synchronized as wave_synchronized
from jarvis.runtime.deferred_effects import DeferredEffects
from jarvis.runtime.media_events import publish_now_playing
from jarvis.integrations.media_state import PlaybackSessionStore, PREFETCH, cache_lock, pin_cache, cache_is_pinned, local_playback_session, MediaRequestSuperseded

logger = logging.getLogger(__name__)

_STARTUP_CACHE_LOCK = threading.Lock()
_STARTUP_CACHE_CLEARED = False

class PlaybackSuperseded(RuntimeError):
    """A newer manual transport command invalidated prepared playback."""


def _local_source_command(method):
    """Admit explicit source requests before lookup/download can block."""
    import functools
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        local = self._source_command_local
        if method.__name__ == "play_youtube_url" and kwargs.get("queue"):
            session = local_playback_session(local_player())
            owner = session.owner()
            if owner is not None and owner is not self:
                return owner.play_youtube_url(*args, **kwargs)
            with self._playback_lock:
                if self._current_track_obj is not None:
                    epoch = session.epoch
                    if not session.queue_current(self, epoch):
                        return self._cancelled_playback_result()
                    local.queue_epoch = epoch
                    local.queue_serial = self._manual_transport_serial
                else:
                    epoch = None
            if epoch is not None:
                try:
                    return method(self, *args, **kwargs)
                except PlaybackSuperseded:
                    return self._cancelled_playback_result()
                finally:
                    local.queue_epoch = None
                    local.queue_serial = None
            # Empty queue starts a source, so use ordinary source admission.
        if getattr(local, "context", None) is not None:
            return method(self, *args, **kwargs)
        session = local_playback_session(local_player())
        with self._playback_lock:
            self._manual_transport_serial += 1
            serial = self._manual_transport_serial
            shared = session.begin(self)
        local.context = (serial, shared)
        try:
            return method(self, *args, **kwargs)
        except PlaybackSuperseded:
            return self._cancelled_playback_result()
        finally:
            local.context = None
            session.finish(shared)
    return wrapped


def _local_playback_request(method):
    """Carry both adapter and shared-player cancellation through preparation."""
    import functools
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        session = local_playback_session(local_player())
        with self._playback_lock:
            context = getattr(self._source_command_local, "context", None)
            if context is not None and kwargs.get("expected_shared_serial") is None:
                kwargs["expected_manual_serial"], kwargs["expected_shared_serial"] = context
            serial = kwargs.get("expected_manual_serial")
            serial = self._manual_transport_serial if serial is None else serial
            self._assert_transport_current(serial)
            shared = kwargs.get("expected_shared_serial")
            outer = shared is None
            if outer:
                if kwargs.get("expected_manual_serial") is not None:
                    shared = session.begin_recovery(self)
                    if shared is None:
                        raise PlaybackSuperseded("Источник изменён или готовится более новая команда")
                else:
                    shared = session.begin(self)
            kwargs["expected_manual_serial"] = serial
            kwargs["expected_shared_serial"] = shared
        try:
            return method(self, *args, **kwargs)
        finally:
            if outer:
                session.finish(shared)
    return wrapped


class WaveUnavailable(RuntimeError):
    """No acceptable Rotor candidate after a bounded scan (not an empty claim)."""


def _abandon_on_failure(method):
    """NEO 177: a start that failed (and was reported as failed) leaves no playback intent behind.

    Before, the station and «auto-advance» stayed set after «Яндекс Музыка недоступна», and the
    player monitor started the music on its own once access or the network came back.
    """
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except PlaybackSuperseded:
            raise
        except Exception:
            try:
                self._abandon_failed_start177()
            except Exception:
                logger.debug("Could not clear a failed Yandex start", exc_info=True)
            raise
    return wrapper



def _norm(value: str) -> str:
    return normalize(value)


def _clean_spoken_track_query(value: str) -> str:
    """Remove voice-command/service scaffolding without touching the song payload.

    This is deliberately conservative: it strips only known command prefixes and
    Yandex Music location phrases.  Misspelled/transliterated song titles are left
    intact and delegated to Yandex search, whose ranking is better suited to them.
    """
    text = str(value or "").strip().strip(" \t\r\n«»\"'")
    if not text:
        return ""
    # Service name can appear before or after the payload when the LLM/parser has
    # passed the original spoken command instead of just the query.
    service = r"(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)"
    text = re.sub(rf"^\s*(?:в|через)?\s*{service}\s*[,.:;—-]*\s*", "", text, flags=re.I)
    text = re.sub(rf"\s+(?:в|через)\s+{service}\s*$", "", text, flags=re.I)
    text = re.sub(rf"^\s*{service}\s*[,.:;—-]*\s*", "", text, flags=re.I)
    text = re.sub(
        r"^\s*(?:(?:джарвис|нео|нио|neo)[\s,.:;—-]+)?(?:включи|поставь|запусти|вруби|воспроизведи|сыграй|найди|поищи|отыщи)\w*\s+",
        "", text, flags=re.I,
    )
    # A full tool payload may be "включи Яндекс Музыка ...".  Strip the service
    # once more after removing the command verb.
    text = re.sub(rf"^\s*(?:в|через)?\s*{service}\s*[,.:;—-]*\s*", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:песн\w*|трек\w*|композиц\w*)\s+", "", text, flags=re.I)
    text = re.sub(r"\s+(?:пожалуйста|плиз)\s*$", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" \t\r\n«»\"'.,:;—-")


def _spoken_query_candidates(value: str) -> list[str]:
    """Generate conservative retry variants for common Russian ASR distortions."""
    base = _clean_spoken_track_query(value)
    if not base:
        return []
    variants = [base]
    normalized = _norm(base)
    compact_normalized = re.sub(r"\s+", "", normalized)
    # Observed ASR repairs are intentionally narrow: they only add retry queries
    # and never overwrite the user's original title. This matters for mixed RU/EN
    # music names, where Whisper often writes an English proper name phonetically.
    if ("король и шут" in normalized or "королишут" in normalized) and any(
        x in normalized for x in ("у тренера свет", "утреннира свет", "утренера свет", "утренерасвет")
    ):
        variants.append("утренний рассвет король и шут")
    if any(x in compact_normalized for x in ("marginstern", "margenstern", "morgenstern", "маргенштерн", "моргенштерн")) and any(
        x in compact_normalized for x in ("dula", "dulo", "dual", "дула", "дуло")
    ):
        variants.extend(["Dulo MORGENSHTERN", "Дуло Моргенштерн"])
    if ("baby doll" in normalized or "babydoll" in normalized) and any(
        x in normalized for x in ("dominic fife", "dominic fike", "доминик файк", "доминик фике")
    ):
        variants.append("Babydoll Dominic Fike")
    if any(x in normalized for x in ("дети рэйв", "дети рейв", "дети рэйф", "дети rave")) and "детств" in normalized:
        variants.extend(["Детство Дети Rave", "Дети Rave Детство"])
    if ("королиш" in compact_normalized or "корольишут" in compact_normalized or "корольишут" in compact_normalized or "король и шут" in normalized) and "воспоминан" in normalized and any(x in normalized for x in ("обалой любви", "о балой любви", "обылой любви", "о былой любви")):
        variants.append("Воспоминания о былой любви Король и Шут")
    # Remove filler tokens that sometimes survive tool routing.
    compact = re.sub(r"\b(?:песню|песня|трек|музыку|мне|пожалуйста)\b", " ", base, flags=re.I)
    compact = re.sub(r"\s+", " ", compact).strip()
    if compact and compact not in variants:
        variants.append(compact)
    return variants


# 186: TrackShort (ID-only or with an embedded Track) is a valid queue item.
# Titles of ID-only items are filled asynchronously into this bounded cache so
# a non-empty queue is never shown as empty.
_TRACK_META_CACHE: dict[str, tuple[str, str]] = {}
_TRACK_META_LOCK = threading.Lock()


class PlaylistAmbiguous(RuntimeError):
    """Several different playlists match the requested name equally well."""

    def __init__(self, requested: str, titles: list[str]):
        self.requested = str(requested or "")
        self.titles = [str(t or "") for t in titles if str(t or "")]
        same = len(set(t.casefold() for t in self.titles)) == 1
        if same:
            text = (f"В вашей Яндекс Музыке несколько плейлистов с названием «{self.titles[0]}». "
                    "Какой включить? Уточните, например, по числу треков или переименуйте один из них.")
        else:
            listed = ", ".join(f"«{t}»" for t in self.titles)
            text = f"Нашёл несколько подходящих плейлистов: {listed}. Какой включить?"
        super().__init__(text)


def _session_event_scope(session: Any) -> str:
    """Stable identity of one shared player session for event ordering."""
    token = getattr(session, "_neo_event_scope", "")
    if not token:
        import uuid
        token = "local:" + uuid.uuid4().hex
        try:
            session._neo_event_scope = token
        except Exception:
            return "local"
    return token


def _embedded_track(track: Any) -> Any:
    if track is None or isinstance(track, dict):
        return track
    embedded = getattr(track, "track", None)
    if embedded is not None and not isinstance(embedded, (str, int)) and (
            getattr(embedded, "title", None) or getattr(embedded, "artists", None)):
        return embedded
    return track


def _cached_track_meta(track: Any) -> tuple[str, str]:
    tid = _track_id(track)
    if not tid:
        return "", ""
    with _TRACK_META_LOCK:
        return _TRACK_META_CACHE.get(tid, ("", ""))


def _remember_track_meta(track_id: str, title: str, artist: str) -> None:
    if not track_id or not (title or artist):
        return
    with _TRACK_META_LOCK:
        if len(_TRACK_META_CACHE) > 4000:
            _TRACK_META_CACHE.clear()
        _TRACK_META_CACHE[str(track_id)] = (str(title or ""), str(artist or ""))


def _artists_text(track: Any) -> str:
    if isinstance(track, dict):
        return str(track.get("artist", "") or track.get("artists", "") or "").strip()
    track = _embedded_track(track)
    artists = getattr(track, "artists", None) or []
    names: list[str] = []
    for artist in artists:
        name = str(getattr(artist, "name", "") or "").strip()
        if name:
            names.append(name)
    if names:
        return ", ".join(names)
    try:
        fn = getattr(track, "artists_name", None)
        if callable(fn):
            value = str(fn() or "").strip()
            if value:
                return value
    except Exception:
        pass
    return _cached_track_meta(track)[1]


def _track_title(track: Any) -> str:
    if isinstance(track, dict):
        return str(track.get("title", "") or track.get("query", "") or "").strip()
    title = str(getattr(_embedded_track(track), "title", "") or "").strip()
    return title or _cached_track_meta(track)[0]


def _track_id(track: Any) -> str:
    if isinstance(track, dict):
        return str(track.get("track_id", "") or track.get("id", "") or "").strip()
    return str(getattr(track, "id", "") or "").strip()


def _track_fingerprint(track: Any) -> str:
    """Stable human-level identity for anti-repeat filtering.

    Yandex can expose the same recording through different Track IDs (album,
    compilation or re-release variants).  My Wave repeat suppression therefore
    uses normalized title+artist in addition to the API Track ID.
    """
    title = _norm(_track_title(track))
    artist = _norm(_artists_text(track))
    if title or artist:
        return f"{title}|{artist}"
    return _track_id(track)


def _flatten_search_tracks(result: Any) -> list[Any]:
    tracks: list[Any] = []
    if result is None:
        return tracks
    track_bucket = getattr(result, "tracks", None)
    if track_bucket is not None:
        for attr in ("results", "items"):
            values = getattr(track_bucket, attr, None)
            if values:
                tracks.extend(list(values))
                break
        if not tracks:
            try:
                tracks.extend(list(track_bucket))
            except Exception:
                pass
    best = getattr(result, "best", None)
    best_result = getattr(best, "result", None) if best is not None else None
    if best_result is not None and hasattr(best_result, "title"):
        if all(_track_id(x) != _track_id(best_result) for x in tracks):
            tracks.insert(0, best_result)
    return tracks


def _similarity(wanted_title: str, wanted_artist: str, track: Any) -> float:
    title = _norm(_track_title(track))
    artist = _norm(_artists_text(track))
    wt = _norm(_clean_spoken_track_query(wanted_title))
    wa = _norm(wanted_artist)
    if not wt:
        return 0.0

    title_score = difflib.SequenceMatcher(None, wt, title).ratio()
    if wt and wt in title:
        title_score = max(title_score, 0.93)

    # Users commonly say "Paparazzi Lady Gaga" as one query.  Older builds
    # compared that whole string only with the title, so a perfectly good Yandex
    # result could be rejected. Compare against title+artist in both orders too.
    combined_a = _norm(f"{title} {artist}")
    combined_b = _norm(f"{artist} {title}")
    combined_score = max(
        difflib.SequenceMatcher(None, wt, combined_a).ratio(),
        difflib.SequenceMatcher(None, wt, combined_b).ratio(),
    )
    wanted_tokens = set(re.findall(r"[a-zа-я0-9]+", wt, flags=re.I))
    result_tokens = set(re.findall(r"[a-zа-я0-9]+", combined_a, flags=re.I))
    token_overlap = (len(wanted_tokens & result_tokens) / max(1, len(wanted_tokens | result_tokens)))
    score = max(title_score * 0.72 + 0.20, combined_score * 0.78 + token_overlap * 0.22)

    if wa:
        artist_score = difflib.SequenceMatcher(None, wa, artist).ratio()
        if wa in artist:
            artist_score = max(artist_score, 0.95)
        score = max(score, title_score * 0.72 + artist_score * 0.28)
    return min(1.0, score)


class YandexMusicAdapter:
    """Background-only Yandex Music integration.

    Search/account operations use the Yandex Music API client and playback is owned
    by NEO's hidden local player.  Desktop Yandex Music, GSMTC and UI automation
    are intentionally not part of this integration anymore.
    """

    def __init__(self, config: Any):
        self.config = config
        self._client_cache: Any | None = None
        self._client_token = ""
        self._api_last_error_kind = ""
        self._api_last_error = ""
        self._api_last_attempt = 0.0
        self._api_lock = threading.RLock()
        self._client_init_lock = threading.RLock()
        self._network_generation = 0
        self._client_generation = 0
        self._feedback_client = None
        self._feedback_token = ""
        self._feedback_generation = -1
        self._feedback_lane = DeferredEffects(self._send_station_feedback, name="NEO-RotorFeedback")
        self._feedback_pending: list[threading.Event] = []
        self._wave_last_completed_id = ""
        self._completed_feedback_key = None
        self._wave_feedback_ready = threading.Event()
        self._wave_feedback_ready.set()
        self._track_batches: dict[int, tuple[Any, str, str]] = {}
        self._track_batch_lock = threading.RLock()
        self._wave_history_lock = history_lock(self._wave_history_path())
        self._wave_queue_generation = 0
        self._playback_generation = 0
        self._playback_lock = threading.RLock()
        self._advance_lock = threading.Lock()
        self._current_track_obj: Any | None = None
        self._current_track_meta: dict[str, Any] = {}
        self._history: list[Any] = []
        self._session_store = PlaybackSessionStore(config, "local")
        saved_session = self._session_store.load()
        self._history = [x for x in saved_session.get("previous", [])[-5:] if isinstance(x, dict)]
        self._prepared_candidates: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._wave_recent_ids: list[str] = []
        self._wave_recent_fingerprints: list[str] = []
        self._wave_recent_entries: list[dict[str, Any]] = []
        self._load_wave_recent_ids()
        self._station = str(saved_session.get("station", ""))
        self._station_batch_id = ""
        self._station_queue: list[Any] = [x for x in saved_session.get("upcoming", [])[:5] if isinstance(x, dict)]
        self._shuffle_enabled = bool(self.config.get("integrations.yandex_music.shuffle_enabled", False))
        self._repeat_mode = str(self.config.get("integrations.yandex_music.repeat_mode", "off") or "off").casefold()
        if self._repeat_mode not in {"off", "track", "playlist"}:
            self._repeat_mode = "off"
        self._playlist_repeat_seed: list[Any] = []
        self._queue_revision = 0
        # When shuffle is enabled while a sequential queue is already playing,
        # remember the old "next" item once. The first shuffled advance must not
        # accidentally pick that same item and make shuffle look like a no-op.
        self._shuffle_avoid_next_token = ""
        self._auto_advance = False
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._prefetch_lock = threading.Lock()
        self._prefetching_ids: set[str] = set()
        self._prefetch_window_generation = 0
        # HOTFIX125: one lazily-started prefetch-window worker per adapter.
        # Rapid next/previous commands update the desired snapshot instead of
        # spawning an unbounded series of overlapping download threads.
        self._prefetch_window_items: list[Any] = []
        self._prefetch_window_event = threading.Event()
        self._prefetch_window_thread: threading.Thread | None = None
        self._prefetch_window_starting: object | None = None
        self._wave_refill_lock = threading.Lock()
        self._wave_refill_active = False
        self._wave_refill_retry_at = 0.0
        self._queue_reported_track = ""
        self._queue_reported_empty = False
        self._queue_reported_count = -1
        self._network_notice_active = False
        self._transition_guard_until = 0.0
        self._manual_transport_guard_until = 0.0
        self._manual_transport_serial = 0
        self._source_command_local = threading.local()
        self._manual_pause_active = False
        self._current_started_monotonic = 0.0
        # HOTFIX75: YouTube is another source for the same hidden PC media
        # transport.  Its signed stream URL is resolved only when an item starts,
        # so long queues do not keep stale expiring CDN URLs.
        self._youtube = YouTubeAudioResolver(config)
        self.clear_transient_cache_on_startup_once()

    def _enabled(self) -> bool:
        return bool(self.config.get("integrations.yandex_music.enabled", True))

    def _api_enabled(self) -> bool:
        return bool(self.config.get("integrations.yandex_music.api_enabled", True))

    def _background_enabled(self) -> bool:
        return bool(self.config.get("integrations.yandex_music.background_playback", True))

    def _strict_non_invasive(self) -> bool:
        return True

    def _ui_fallback(self) -> bool:
        return False

    def _protocol_fallback(self) -> bool:
        return False

    def _background_volume_percent(self) -> int:
        try:
            raw = self.config.get("integrations.yandex_music.background_volume_percent", 100)
            return max(0, min(100, int(100 if raw is None else raw)))
        except Exception:
            return 100

    def _persist_background_volume(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        try:
            data = getattr(self.config, "data", None)
            save = getattr(self.config, "save", None)
            if isinstance(data, dict):
                data.setdefault("integrations", {}).setdefault("yandex_music", {})["background_volume_percent"] = percent
                if callable(save):
                    save(data)
        except Exception as exc:
            logger.debug("Could not persist Yandex background volume: %s", exc)

    def set_background_volume(self, percent: int | float) -> dict[str, Any]:
        value = max(0, min(100, int(round(float(percent)))))
        data = local_player().set_volume(value)
        self._persist_background_volume(value)
        return {**data, "app": "yandex_music", "volume_percent": value, "method": "jarvis_background_qt_volume", "non_invasive": True}

    def adjust_background_volume(self, delta: int) -> dict[str, Any]:
        status = self._background_status() or {}
        current = status.get("volume_percent")
        if current is None and "volume" in status:
            try:
                current = int(round(float(status.get("volume", 1.0)) * 100.0))
            except Exception:
                current = None
        if current is None:
            current = self._background_volume_percent()
        return self.set_background_volume(int(current) + int(delta))

    @staticmethod
    def _cache_root() -> Path:
        configured = str(os.getenv("NEOJARVIS_CACHE_DIR", "") or "").strip()
        if configured:
            root = Path(configured) / "yandex_music"
        else:
            base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or tempfile.gettempdir()
            root = Path(base) / "NeoJarvis" / "cache" / "yandex_music"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _safe_cache_name(value: str) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
        return cleaned.strip("._") or "track"

    @classmethod
    def clear_transient_cache(cls) -> dict[str, int]:
        """Remove temporary Yandex audio files left by previous JARVIS runs.

        Discord uses this directory only as a short-lived handoff between the
        Yandex provider and FFmpeg.  Clearing it at process startup prevents
        old tracks or interrupted ``.part`` downloads from accumulating.
        Playback that is already running is never touched because this method
        is called once during application bootstrap.
        """
        root = cls._cache_root()
        removed = 0
        removed_bytes = 0
        for item in list(root.iterdir()):
            if not item.is_file():
                continue
            # Finished cache entries are reusable across restarts. Only abandoned
            # partial downloads and expired audio are transient garbage.
            if not item.name.endswith(".part"):
                try:
                    if time.time() - item.stat().st_mtime < 7 * 86400:
                        continue
                except OSError:
                    continue
            try:
                size = int(item.stat().st_size)
            except Exception:
                size = 0
            try:
                item.unlink()
                removed += 1
                removed_bytes += size
            except Exception as exc:
                logger.debug("Could not remove stale Yandex cache file %s: %s", item, exc)
        return {"removed_files": removed, "removed_bytes": removed_bytes}

    @classmethod
    def clear_transient_cache_on_startup_once(cls) -> dict[str, int]:
        global _STARTUP_CACHE_CLEARED
        if _STARTUP_CACHE_CLEARED:
            return {"removed_files": 0, "removed_bytes": 0}
        with _STARTUP_CACHE_LOCK:
            if _STARTUP_CACHE_CLEARED:
                return {"removed_files": 0, "removed_bytes": 0}
            result = cls.clear_transient_cache()
            _STARTUP_CACHE_CLEARED = True
            if result["removed_files"]:
                logger.info(
                    "Yandex transient audio cache cleared on JARVIS startup: %s files, %.1f MiB",
                    result["removed_files"], result["removed_bytes"] / (1024 * 1024),
                )
            return result

    def _trim_cache(self, limit_bytes: int = 1536 * 1024 * 1024) -> None:
        try:
            files = [item for item in self._cache_root().iterdir() if item.is_file() and not item.name.endswith(".part")]
            total = sum(item.stat().st_size for item in files)
            if total <= limit_bytes:
                return
            for item in sorted(files, key=lambda x: x.stat().st_mtime):
                if cache_is_pinned(item):
                    continue
                size = item.stat().st_size
                try:
                    item.unlink()
                    total -= size
                except Exception:
                    pass
                if total <= int(limit_bytes * 0.80):
                    break
        except Exception:
            pass

    def _cached_candidate_path(self, candidate: dict[str, Any]) -> str:
        """Return a ready cached file without ever waiting for the network."""
        track_id = self._safe_cache_name(str(candidate.get("id", "") or "track"))
        codec = self._safe_cache_name(str(candidate.get("codec", "") or "audio").casefold())
        bitrate = int(candidate.get("bitrate_kbps", 0) or 0)
        target = self._cache_root() / f"{track_id}_{bitrate}.{codec}"
        try:
            if target.exists() and target.stat().st_size >= 64 * 1024:
                try:
                    os.utime(target, None)
                except Exception:
                    pass
                return str(target)
        except Exception:
            return ""
        return ""

    def _cache_candidate(self, candidate: dict[str, Any]) -> str:
        key = f"{candidate.get('id')}:{candidate.get('codec')}:{candidate.get('bitrate_kbps')}"
        with cache_lock(key):
            return self._cache_candidate_locked(candidate)

    def _cache_candidate_locked(self, candidate: dict[str, Any]) -> str:
        """Download a complete short-lived Yandex stream to a local cache file.

        Playing a completed local file avoids a temporary CDN URL expiring or a
        transient network reset halfway through a song.  A .part file is renamed
        atomically only after the download finishes.
        """
        url = str(candidate.get("url", "") or "").strip()
        if not url:
            raise RuntimeError("Пустая ссылка аудиопотока Яндекс Музыки")
        track_id = self._safe_cache_name(str(candidate.get("id", "") or "track"))
        codec = self._safe_cache_name(str(candidate.get("codec", "") or "audio").casefold())
        bitrate = int(candidate.get("bitrate_kbps", 0) or 0)
        target = self._cache_root() / f"{track_id}_{bitrate}.{codec}"
        cached = self._cached_candidate_path(candidate)
        if cached:
            return cached
        part = target.with_suffix(target.suffix + f".{os.getpid()}.{threading.get_ident()}.part")
        try:
            if part.exists():
                part.unlink()
        except Exception:
            pass
        request = urllib.request.Request(url, headers={"User-Agent": "NEO-JARVIS/4.0.10"})
        total = 0
        expected_bytes = 0
        deadline = time.monotonic() + 60.0
        with urllib.request.urlopen(request, timeout=12.0) as response, part.open("wb") as output:
            try:
                expected_bytes = int(response.headers.get("Content-Length", "0") or 0)
            except Exception:
                expected_bytes = 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Yandex audio preparation exceeded its 60 second budget")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                total += len(chunk)
                if total > 256 * 1024 * 1024:
                    raise RuntimeError("Аудиофайл Яндекс Музыки неожиданно превышает 256 МБ")
        # A TCP/CDN connection can occasionally close cleanly from urllib's point
        # of view after only part of the object was received.  If the origin gave
        # us a Content-Length, never promote such a partial .part file to cache.
        if expected_bytes > 0 and total < expected_bytes:
            try:
                part.unlink()
            except Exception:
                pass
            raise RuntimeError(
                f"Аудиопоток Яндекс Музыки загрузился не полностью: {total} из {expected_bytes} байт"
            )
        if total < 64 * 1024:
            try:
                part.unlink()
            except Exception:
                pass
            raise RuntimeError(f"Яндекс Музыка вернула слишком короткий аудиофайл ({total} байт)")
        os.replace(part, target)
        self._trim_cache()
        return str(target)

    @staticmethod
    def _yandex_error_kind(exc: Exception) -> str:
        """Classify API failures without confusing a slow network with a bad token."""
        name = type(exc).__name__.casefold()
        message = str(exc or "").casefold()
        if any(x in name for x in ("timeout", "timedout", "network", "connection")) or any(
            x in message for x in ("timed out", "timeout", "connection reset", "connection aborted", "temporarily unavailable", "network error", "network is unreachable", "network unreachable", "host not found", "name resolution", "getaddrinfo", "remote host closed", "ошибка сети", "нарушение работы сети", "задержка сети")
        ):
            return "network"
        if any(x in name for x in ("unauthor", "forbidden", "invalidtoken")) or any(
            x in message for x in ("401", "403", "unauthorized", "forbidden", "invalid token", "token is invalid")
        ):
            return "auth"
        return "api"

    def _api_unavailable_message(self, feature: str = "API Яндекс Музыки") -> str:
        token_present = bool(yandex_music_token(self.config))
        kind = str(getattr(self, "_api_last_error_kind", "") or "")
        detail = str(getattr(self, "_api_last_error", "") or "").strip()
        if not token_present:
            return f"Для {feature} нужен OAuth-токен Яндекс Музыки. Откройте Настройки → Медиа → Яндекс Музыка и нажмите «Подключить Яндекс Музыку»."
        if kind == "network":
            return "Обнаружена высокая задержка или нарушение работы сети."
        if kind == "auth":
            return f"Яндекс отклонил сохранённый OAuth-токен для {feature}. Только в этом случае требуется переподключить Яндекс Музыку в настройках."
        suffix = f" Последняя ошибка: {detail}" if detail else ""
        return f"OAuth-токен Яндекс Музыки сохранён, но {feature} временно недоступен.{suffix}"

    def _api_client(self) -> Any | None:
        # Prefetch and foreground commands can arrive together after a network
        # change. Share initialization instead of opening competing API clients.
        # Initialization and metadata HTTP requests have different locks. A
        # cached-client lookup must not wait behind a background Rotor request.
        with self._client_init_lock:
            if self._client_generation != self._network_generation:
                self._client_cache = None
                self._client_token = ""
                self._api_last_attempt = 0.0
                self._client_generation = self._network_generation
            if (self._client_cache is None and self._api_last_error_kind == "network"
                    and time.monotonic() - self._api_last_attempt < 3.0):
                return None
            return self._api_client_locked()

    def _api_client_locked(self) -> Any | None:
        if not self._api_enabled():
            self._api_last_error_kind = "disabled"
            self._api_last_error = "API integration disabled"
            return None
        token = yandex_music_token(self.config)
        if not token:
            self._api_last_error_kind = "missing_token"
            self._api_last_error = ""
            return None
        if self._client_cache is not None and token == self._client_token:
            return self._client_cache

        # HOTFIX36: HOTFIX35 authenticated correctly but the normal playback
        # adapter then created a fresh Client(token).init() with the library's
        # much shorter default request timeout. On slower connections that call
        # timed out and the old code incorrectly presented the already-saved
        # token as invalid. Use an explicit long timeout and retry initialization.
        try:
            from yandex_music import Client
            from yandex_music.utils.request import Request
        except Exception as exc:
            self._api_last_error_kind = "api"
            self._api_last_error = str(exc or type(exc).__name__)
            logger.warning("Yandex Music API client import failed: %s", exc)
            return None

        last_exc: Exception | None = None
        for attempt in range(1, 3):
            try:
                # NEO 174: one kept-alive connection instead of a new one per API call.
                from jarvis.integrations.yandex_pool174 import make_request
                request = make_request(self.config, timeout=10)
                client = Client(token, request=request)
                initialized = client.init()
                if initialized is not None:
                    client = initialized
                self._client_cache = client
                self._client_token = token
                self._api_last_error_kind = ""
                self._api_last_error = ""
                self._api_last_attempt = time.monotonic()
                if attempt > 1:
                    logger.info("Yandex Music API initialized after retry %s/2", attempt)
                return client
            except Exception as exc:
                last_exc = exc
                kind = self._yandex_error_kind(exc)
                self._api_last_error_kind = kind
                self._api_last_error = str(exc or type(exc).__name__)
                self._api_last_attempt = time.monotonic()
                logger.warning(
                    "Yandex Music API initialization attempt %s/2 failed (%s); saved token is retained: %s",
                    attempt, kind, exc,
                )
                # An explicit auth rejection will not become valid through a
                # network retry. Transport/server failures are retried.
                if kind == "auth":
                    break
                if attempt < 2:
                    time.sleep(0.3)

        # Never erase/replace token_encrypted here. A transport timeout says
        # nothing about credential validity; settings remain connected.
        if last_exc is not None and self._api_last_error_kind == "network":
            logger.warning("Yandex Music API timed out; OAuth token remains saved for a later retry")
        return None

    def api_status(self) -> dict[str, Any]:
        token_present = bool(yandex_music_token(self.config))
        if not self._api_enabled():
            return {"enabled": False, "token": token_present, "available": False, "error_kind": "disabled"}
        client = self._api_client()
        return {
            "enabled": True,
            "token": token_present,
            "available": client is not None,
            "error_kind": str(self._api_last_error_kind or ""),
            "error": str(self._api_last_error or ""),
        }

    def open(self) -> dict[str, Any]:
        raise RuntimeError(
            "UI-интеграция Яндекс Музыки удалена. Для музыки NEO использует только фоновый API/player; "
            "если нужно просто открыть приложение, используйте обычную команду открытия приложения."
        )

    def _background_status(self) -> dict[str, Any] | None:
        try:
            return local_player().status_if_running()
        except Exception:
            return None

    def status(self) -> dict[str, Any]:
        session = local_playback_session(local_player())
        owner = session.owner()
        if owner is not None and owner is not self:
            return owner.status()
        if session.source_epoch and owner is None:
            raise RuntimeError("Текущий источник фонового плеера больше недоступен")
        background = self._background_status()
        with self._playback_lock:
            meta = dict(self._current_track_meta)
            station = self._station
        if background and (background.get("url") or meta):
            return {
                **background,
                "app": "yandex_music",
                "title": str(background.get("title", "") or meta.get("title", "")),
                "artist": str(background.get("artist", "") or meta.get("artist", "")),
                "track_id": str(meta.get("id", "") or ""),
                "station": station,
                "shuffle": bool(self._shuffle_enabled),
                "repeat_mode": str(self._repeat_mode),
                "method": "jarvis_background_qt",
                "volume_percent": int(round(float(background.get("volume", self._background_volume_percent() / 100.0)) * 100.0)),
                "non_invasive": True,
                "api": ({"checked": False, "required": False} if self._content_provider(self._current_track_obj) == "youtube" else self.api_status()),
            }
        return {
            "app": "yandex_music", "state": "stopped", "playing": False,
            "shuffle": bool(self._shuffle_enabled),
            "repeat_mode": str(self._repeat_mode),
            "method": "jarvis_background_qt", "non_invasive": True, "api": self.api_status(),
        }

    @staticmethod
    def _content_provider(track: Any) -> str:
        if isinstance(track, dict):
            if isinstance(track.get("track"), dict):
                track = track["track"]
            if (track.get("_source_type") == "youtube" or track.get("content_provider") == "youtube"
                    or str(track.get("media_source", "")).casefold() == "youtube"):
                return "youtube"
        return "yandex_music"

    def playback_provenance(self) -> dict[str, Any]:
        """Passive content identity; transport ownership is never a source label."""
        owner = local_playback_session(local_player()).owner()
        if owner is not None and owner is not self:
            return owner.playback_provenance()
        if self._playback_lock.acquire(blocking=False):
            try:
                track = self._current_track_obj or self._current_track_meta
                provider = self._content_provider(track)
                self._content_provider_snapshot = provider
            finally:
                self._playback_lock.release()
        else:
            provider = getattr(self, "_content_provider_snapshot", "yandex_music")
        return {"transport_owner": "yandex_music", "content_provider": provider,
                "active_content_provider": provider,
                "media_source": "YouTube" if provider == "youtube" else "Yandex Music"}

    @staticmethod
    def _queue_row(track: Any) -> dict[str, Any]:
        if isinstance(track, dict) and isinstance(track.get("track"), dict):
            track = track.get("track")
        return {
            "id": _track_id(track),
            "title": _track_title(track),
            "artist": _artists_text(track),
            "content_provider": YandexMusicAdapter._content_provider(track),
            "media_source": "YouTube" if YandexMusicAdapter._content_provider(track) == "youtube" else "Yandex Music",
        }

    def queue_status(self, *, limit: int = 20) -> dict[str, Any]:
        """Return the real in-memory computer music queue without network I/O."""
        session = local_playback_session(local_player())
        owner = session.owner()
        if owner is not None and owner is not self:
            return owner.queue_status(limit=limit)
        if session.source_epoch and owner is None:
            raise RuntimeError("Текущий источник фонового плеера больше недоступен")
        with self._playback_lock:
            queue = list(self._station_queue)
            current = dict(self._current_track_meta)
            station = str(self._station or "")
            shuffle = bool(self._shuffle_enabled)
            repeat_mode = str(self._repeat_mode)
            auto_advance = bool(self._auto_advance)
        rows = [self._queue_row(item) for item in queue[:max(1, min(100, int(limit or 20)))]]
        rows = [row for row in rows if row.get("title") or row.get("id")]
        missing = [item for item in queue[:8] if _track_id(item) and not _track_title(item)
                   and not self._is_youtube_entry(item)]
        if missing:
            # Names of ID-only TrackShort items arrive asynchronously; the queue
            # is reported as loading, never as empty.
            self._request_queue_metadata(missing)
        # HOTFIX127: a status/dashboard read must stay non-blocking, but an
        # unexpectedly shallow My Wave queue is a useful self-heal signal.
        # Refill asynchronously; never make Telegram/Discord/UI status wait on
        # Rotor network I/O.
        if station == "user:onyourwave" and len(queue) < 5:
            try:
                self._ensure_wave_queue_depth(8)
            except Exception:
                pass
        try:
            transport = local_player()
            cached_status = getattr(transport, "cached_status", None)
            player = cached_status() if callable(cached_status) else transport.status_if_running()
            player = player or {"state": "stopped"}
        except Exception:
            player = {"state": "unavailable"}
        return {
            "app": "yandex_music", "verified": True, "method": "jarvis_background_queue",
            "current": current, "queue": rows, "queue_length": len(queue),
            "station": station, "shuffle": shuffle, "repeat_mode": repeat_mode, "auto_advance": auto_advance,
            "queue_refilling": bool(self._wave_refill_active),
            "queue_metadata_pending": len(missing),
            "queue_revision": int(getattr(self, "_queue_revision", 0)),
            "queue_unavailable": bool(station == "user:onyourwave" and not rows),
            "player": player, "non_invasive": True, **self.playback_provenance(),
        }

    def _request_queue_metadata(self, items: list[Any]) -> None:
        """Fetch titles of ID-only queue items off the caller thread (bounded)."""
        ids = []
        with _TRACK_META_LOCK:
            inflight = getattr(self, "_metadata_inflight", None)
            if inflight is None:
                inflight = self._metadata_inflight = set()
            for item in items:
                tid = _track_id(item)
                if tid and tid not in inflight:
                    inflight.add(tid)
                    ids.append(tid)
        if not ids:
            return
        revision = int(getattr(self, "_queue_revision", 0))

        def worker() -> None:
            filled = False
            try:
                client = self._api_client()
                if client is None:
                    return
                with self._api_lock:
                    tracks = client.tracks(ids) or []
                for track in tracks:
                    tid = _track_id(track)
                    title = str(getattr(track, "title", "") or "").strip()
                    if tid and title:
                        _remember_track_meta(tid, title, _artists_text(track))
                        filled = True
            except Exception as exc:
                logger.debug("Queue metadata fetch failed: %s", type(exc).__name__)
            finally:
                with _TRACK_META_LOCK:
                    self._metadata_inflight.difference_update(ids)
            if filled:
                self._publish_queue_update(expected_revision=revision)

        threading.Thread(target=worker, name="NEO-QueueMetadata", daemon=True).start()

    def _reconciled_queue(self, prepared: list[Any], revision: int | None, reconcile: Any) -> list[Any]:
        """MEDIA185-U14: a transition planned on an older queue revision must not
        overwrite a newer confirmed edit (shuffle, clear, add). The live queue
        wins; only the occurrence this transition consumed is removed from it.
        Caller holds the playback lock."""
        if revision is None or int(getattr(self, "_queue_revision", 0)) == int(revision):
            return prepared
        live = list(self._station_queue)
        if callable(reconcile):
            try:
                return list(reconcile(live))
            except Exception:
                logger.debug("Queue reconcile failed; keeping the live queue", exc_info=True)
        return live

    @staticmethod
    def _without_occurrence(values: list[Any], consumed: Any) -> list[Any]:
        for index, item in enumerate(values):
            if item is consumed:
                return values[:index] + values[index + 1:]
        cid = _track_id(consumed)
        for index, item in enumerate(values):
            if cid and _track_id(item) == cid:
                return values[:index] + values[index + 1:]
        return values

    def _bump_queue_revision(self) -> int:
        """Every committed queue edit gets a new revision (caller holds the lock)."""
        self._queue_revision = int(getattr(self, "_queue_revision", 0)) + 1
        return self._queue_revision

    def _publish_queue_update(self, *, expected_revision: int | None = None) -> None:
        """Tell the UI that the current playback's queue projection changed."""
        with self._playback_lock:
            if expected_revision is not None and int(getattr(self, "_queue_revision", 0)) != expected_revision:
                return
            current = dict(self._current_track_meta)
            generation = self._playback_generation
            paused = bool(self._manual_pause_active)
        title = str(current.get("title", "") or "")
        if not title:
            return
        self._after_playback_effect("queue_update_notice", publish_now_playing,
            "youtube" if str(current.get("media_source", "")) == "YouTube" else "yandex_music",
            title, str(current.get("artist", "")), str(current.get("id", "")),
            reason="queue_updated", playing=not paused, playback_generation=generation,
            announce=False)

    def mark_queue_reported(self, track_id: str, has_rows: bool, *, known_count: int | None = None) -> list[dict[str, Any]] | None:
        """Remember an actual answer so a late refill can supply its missing queue."""
        with self._playback_lock:
            current_id = _track_id(self._current_track_obj) or str(self._current_track_meta.get("id", ""))
            if str(track_id or "") != current_id:
                return None
            self._queue_reported_track = current_id
            # Refill can finish between the snapshot and formatting. Return the
            # ready queue directly so neither its notification nor its data is lost.
            ready = [self._queue_row(x) for x in self._station_queue[:5]]
            reported = min(5, max(0, int(known_count))) if known_count is not None else (len(ready) if has_rows else 0)
            self._queue_reported_empty = not bool(has_rows or ready)
            self._queue_reported_count = max(reported, len(ready))
            return ready if len(ready) > reported else None

    def _note_network_failure(self, exc: Exception) -> bool:
        if self._yandex_error_kind(exc) != "network":
            return False
        self._api_last_error_kind = "network"
        self._api_last_error = str(exc)
        with self._playback_lock:
            notify = not self._network_notice_active
            self._network_notice_active = True
            current = dict(self._current_track_meta)
        if notify and current.get("title"):
            self._after_playback_effect("network_notice", publish_now_playing,
                "yandex_music", str(current["title"]), str(current.get("artist", "")),
                str(current.get("id", "")), reason="network_unavailable", playing=False)
        return True

    def _queue_preview_state(self, *, queue_limit: int = 8, history_limit: int = 5) -> dict[str, Any]:
        """Cheap in-memory queue/history snapshot for transport acknowledgements."""
        with self._playback_lock:
            current = dict(self._current_track_meta)
            queue = list(self._station_queue)[:max(1, int(queue_limit))]
            history = list(self._history)[-max(1, int(history_limit)):]
        return {
            "current": current,
            "queue": [self._queue_row(x) for x in queue],
            "queue_length": len(queue),
            "history": [self._queue_row(x) for x in history],
        }

    def queue_clear(self) -> dict[str, Any]:
        """Clear upcoming tracks while allowing the current song to finish."""
        with self._playback_lock:
            removed = len(self._station_queue)
        self._clear_background_sequence(keep_current=True)
        self._publish_queue_update()
        snapshot = self.queue_status()
        snapshot.update({"cleared": True, "removed": removed, "verified": True})
        return snapshot

    def _abandon_failed_start177(self) -> None:
        """Forget the station/auto-advance of a start that failed, unless music really plays."""
        try:
            status = local_player().status_if_running() or {}
        except Exception:
            status = {}
        if str(status.get("state", "") or "").casefold() == "playing" and not status.get("error"):
            return
        self._clear_background_sequence(keep_current=False)
        logger.info("Yandex start failed: the playback request is dropped (it will not start later on its own)")

    def _clear_background_sequence(self, *, keep_current: bool = True) -> None:
        with self._playback_lock:
            self._manual_transport_serial += 1
            self._station = ""
            self._station_batch_id = ""
            self._station_queue = []
            self._playlist_repeat_seed = []
            self._shuffle_avoid_next_token = ""
            self._auto_advance = False
            self._bump_queue_revision()
            if not keep_current:
                self._current_track_obj = None
                self._current_track_meta = {}
            # Retire exactly the monitor generation that owned this sequence.
            self._monitor_stop.set()
        self._wave_queue_generation += 1
        self._persist_playback()

    def transport(self, action: str) -> dict[str, Any]:
        try:
            session = local_playback_session(local_player())
            with session.lock:
                entry_epoch = session.epoch
                owner = session.owner() or session.preparing_owner() or session.submitted_owner()
                unavailable = bool(session.source_epoch and owner is None)
            if unavailable:
                raise RuntimeError("Текущий источник фонового плеера больше недоступен")
            target = owner if owner is not None else self
            return target._transport_local(action, expected_entry_epoch=entry_epoch)
        except PlaybackSuperseded:
            return self._cancelled_playback_result()

    def _transport_local(self, action: str, *, expected_entry_epoch: int | None = None) -> dict[str, Any]:
        action = str(action or "").strip().casefold()
        session = local_playback_session(local_player())
        if action in {"stop", "pause"} and (session.preparing_owner() is self or session.submitted_owner() is self):
            with self._playback_lock:
                has_current = self._current_track_obj is not None or bool(self._current_track_meta)
                if not has_current:
                    session, shared = self._begin_local_transport(local_player(), expected_epoch=expected_entry_epoch)
                    self._manual_transport_serial += 1
                    operation_serial = self._manual_transport_serial
                    self._manual_pause_active = True
                    submitted = session.submitted_owner() is self
            if not has_current:
                try:
                    if submitted:
                        data = self._guarded_player_transport(local_player(), action,
                            lambda: self._assert_shared_current(operation_serial, shared))
                        self._assert_shared_current(operation_serial, shared)
                        if not data.get("verified"):
                            raise RuntimeError("Фоновый плеер не подтвердил остановку незавершённого запуска")
                        if action == "stop":
                            session.mark_stopped(shared)
                        return {**data, "app": "yandex_music", "cancelled_preparation": True, "non_invasive": True}
                    return {"app": "yandex_music", "verified": True, "cancelled_preparation": True,
                            "state": "stopped", "non_invasive": True}
                finally:
                    session.finish(shared)
        if not self._enabled() and not self.local_youtube_transport_allowed(action):
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        action = str(action or "").strip().casefold()
        if action not in {"play_pause", "play", "pause", "next", "previous", "stop"}:
            raise ValueError(f"Неподдерживаемая команда Яндекс Музыки: {action}")

        player = local_player()
        background = player.status_if_running()
        with self._playback_lock:
            owns_background_track = self._current_track_obj is not None or bool(self._current_track_meta)

        # JARVIS' own headless player is authoritative whenever it owns a track.
        # Do not route its pause/next commands to Windows GSMTC: this player has
        # no desktop media session by design.
        if owns_background_track or (background and background.get("url")):
            state = str((background or {}).get("state", "") or "").casefold()
            if action == "play_pause":
                action = "play" if state == "paused" else "pause"
            if action == "play":
                with self._playback_lock:
                    session, shared_serial = self._begin_local_transport(player, expected_epoch=expected_entry_epoch)
                    self._manual_transport_serial += 1
                    self._manual_pause_active = False
                    self._manual_transport_guard_until = time.monotonic() + 2.5
                    resume_track = self._current_track_obj
                    resume_station = self._station
                    resume_batch = self._station_batch_id
                    resume_queue = list(self._station_queue)
                    resume_auto = self._auto_advance
                    operation_serial = self._manual_transport_serial
                try:
                    try:
                        if session.needs_source_rebuild():
                            data = {"verified": False, "source_rebuild_required": True}
                        else:
                            data = self._guarded_player_transport(player, "resume", lambda: self._assert_shared_current(operation_serial, shared_serial))
                    except PlaybackSuperseded:
                        raise
                    except Exception as exc:
                        logger.warning("Yandex resume of loaded source failed; rebuilding current source: %s", exc)
                        data = {"verified": False, "error": str(exc)}
                    if not bool(data.get("verified", False)) and resume_track is not None:
                        # Qt can leave the worker in Stopped/InvalidMedia after a CDN
                        # hiccup or a failed next-source replacement. Re-open the
                        # authoritative current Track object instead of telling the
                        # user that there is no active music.
                        logger.warning("Yandex resume did not advance timeline; reopening current track")
                        data = self._play_track_object(
                            resume_track, expected_manual_serial=operation_serial, expected_shared_serial=shared_serial,
                            station=resume_station, batch_id=resume_batch,
                            queue=resume_queue, auto_advance=resume_auto,
                            remember_previous=False, fast_transition=True,
                        )
                    self._assert_shared_current(operation_serial, shared_serial)
                    if not bool(data.get("verified", False)):
                        raise RuntimeError("Фоновый плеер не подтвердил фактическое продолжение трека")
                    self._commit_local_source(operation_serial, shared_serial)
                    logger.info("Yandex background transport resumed by user")
                    return {**data, "app": "yandex_music", "method": "jarvis_background_qt", "non_invasive": True}
                finally:
                    session.finish(shared_serial)
            if action == "pause":
                # Mark the pause before asking Qt to pause. Some backends briefly
                # report Stopped/EndOfMedia around a state transition; the station
                # monitor must never interpret that as permission to auto-next.
                with self._playback_lock:
                    session, shared_serial = self._begin_local_transport(player, expected_epoch=expected_entry_epoch)
                    self._manual_transport_serial += 1
                    self._manual_pause_active = True
                    self._manual_transport_guard_until = time.monotonic() + 8.0
                    operation_serial = self._manual_transport_serial
                try:
                    data = self._guarded_player_transport(player, "pause", lambda: self._assert_shared_current(operation_serial, shared_serial))
                    self._assert_shared_current(operation_serial, shared_serial)
                finally:
                    session.finish(shared_serial)
                if not bool(data.get("verified", False)):
                    raise RuntimeError("Фоновый плеер не подтвердил фактическую паузу")
                logger.info("Yandex background transport paused by user; auto-advance suspended")
                return {**data, "app": "yandex_music", "method": "jarvis_background_qt", "non_invasive": True}
            if action == "stop":
                # Stop the transport without destroying the selected station or
                # pending My Wave queue. A later Play can resume/rebuild the same
                # source instead of forcing the user to say «включи Мою волну»
                # again. Manual-stop guard also prevents the station monitor from
                # treating Qt's Stopped state as end-of-track auto-advance.
                with self._playback_lock:
                    session, shared_serial = self._begin_local_transport(player, expected_epoch=expected_entry_epoch)
                    self._manual_transport_serial += 1
                    self._manual_pause_active = True
                    self._manual_transport_guard_until = time.monotonic() + 30.0
                    operation_serial = self._manual_transport_serial
                try:
                    data = self._guarded_player_transport(player, "stop", lambda: self._assert_shared_current(operation_serial, shared_serial))
                    self._assert_shared_current(operation_serial, shared_serial)
                finally:
                    session.finish(shared_serial)
                if not bool(data.get("verified", False)):
                    raise RuntimeError("Фоновый плеер не подтвердил фактическую остановку")
                session.mark_stopped(shared_serial)
                return {**data, "app": "yandex_music", "method": "jarvis_background_qt", "non_invasive": True}
            if action == "next":
                with self._playback_lock:
                    claimed = self._advance_lock.acquire(blocking=False)
                    active = getattr(self, "_advance_operation", None)
                    session = local_playback_session(player)
                    if (not claimed and active and active[0] == "next"
                            and active[1] == self._manual_transport_serial
                            and session.current(active[2])):
                        return self._coalesced_next_result()
                    try:
                        session, shared_serial = self._begin_local_transport(player, expected_epoch=expected_entry_epoch)
                    except Exception:
                        if claimed:
                            self._advance_lock.release()
                        raise
                    self._manual_pause_active = False
                    self._manual_transport_serial += 1
                    self._manual_transport_guard_until = time.monotonic() + 15.0
                    operation_serial = self._manual_transport_serial
                # A previous Next invalidated by Stop is not a live operation to
                # coalesce onto. Wait for its bounded cleanup, preserving this
                # newer request's token; a later Stop can cancel this waiter too.
                handed_off = False
                try:
                    while not claimed:
                        self._assert_shared_current(operation_serial, shared_serial)
                        claimed = self._advance_lock.acquire(timeout=0.025)
                    self._assert_shared_current(operation_serial, shared_serial)
                    with self._playback_lock:
                        self._advance_operation = ("next", operation_serial, shared_serial)
                    handed_off = True
                    return self._next_background(auto=False, expected_manual_serial=operation_serial,
                        expected_shared_serial=shared_serial, advance_claimed=True)
                finally:
                    if not handed_off:
                        session.finish(shared_serial)
                        if claimed:
                            self._advance_lock.release()
            if action == "previous":
                with self._playback_lock:
                    if not self._advance_lock.acquire(blocking=False):
                        raise RuntimeError("Переключение трека уже выполняется")
                    try:
                        session, shared_serial = self._begin_local_transport(player, expected_epoch=expected_entry_epoch)
                    except Exception:
                        self._advance_lock.release()
                        raise
                    self._manual_pause_active = False
                    self._manual_transport_serial += 1
                    self._manual_transport_guard_until = time.monotonic() + 15.0
                    operation_serial = self._manual_transport_serial
                    self._advance_operation = ("previous", operation_serial, shared_serial)
                return self._previous_background(expected_manual_serial=operation_serial, expected_shared_serial=shared_serial, advance_claimed=True)

        raise RuntimeError(
            "Фоновый плеер Яндекс Музыки сейчас не активен. Desktop/UI/GSMTC fallback отключены; "
            "сначала включите трек или «Мою волну» через NEO."
        )

    def _materialize_track(self, track: Any) -> Any:
        """Return a playable queue object, materializing Yandex TrackShort only."""
        if track is None:
            return track
        if isinstance(track, dict) and str(track.get("_source_type", "") or "").casefold() == "youtube":
            return track
        if isinstance(track, dict) and track.get("_source_type") == "yandex_saved":
            if track.get("_live_track") is not None:
                return track["_live_track"]
            client = self._api_client()
            if client is None:
                raise RuntimeError(self._api_unavailable_message())
            tracks = client.tracks([str(track.get("id") or track.get("track_id"))])
            if not tracks:
                raise RuntimeError("Сохранённый трек больше недоступен в каталоге Яндекс Музыки")
            return tracks[0]
        embedded = getattr(track, "track", None)
        if embedded is not None and hasattr(embedded, "get_download_info"):
            return embedded
        if hasattr(track, "get_download_info"):
            return track
        fetch = getattr(track, "fetch_track", None)
        if callable(fetch):
            return fetch()
        # Compatibility with older yandex-music-api naming used in some releases.
        fetch = getattr(track, "fetchTrack", None)
        if callable(fetch):
            return fetch()
        return track

    def _liked_tracks(self) -> list[Any]:
        client = self._api_client()
        if client is None:
            raise RuntimeError(self._api_unavailable_message())

        def fetch() -> Any:
            with self._api_lock:
                return client.users_likes_tracks()

        result = self._retry(fetch, "liked tracks")
        if result is None:
            return []
        values = getattr(result, "tracks", None)
        if values is not None:
            return [item for item in list(values) if item is not None]
        try:
            return [item for item in list(result) if item is not None]
        except Exception:
            return []

    def liked_tracks(self) -> list[Any]:
        """Public, API-only view of the authenticated user's «Мои треки».

        Discord uses this to build its own voice queue without launching or
        controlling the Yandex desktop application.
        """
        return list(self._liked_tracks())

    @staticmethod
    def _playlist_title(playlist: Any) -> str:
        return str(getattr(playlist, "title", "") or getattr(playlist, "name", "") or "").strip()

    def _user_playlists(self) -> list[Any]:
        """Return playlists owned/visible in the authenticated user's library."""
        client = self._api_client()
        if client is None:
            raise RuntimeError(self._api_unavailable_message())

        def fetch() -> Any:
            with self._api_lock:
                method = getattr(client, "users_playlists_list", None)
                if callable(method):
                    return method()
                kinds_method = getattr(client, "users_playlists_kinds", None)
                by_kind = getattr(client, "users_playlists", None)
                if callable(kinds_method) and callable(by_kind):
                    raw_kinds = kinds_method()
                    values = getattr(raw_kinds, "kinds", raw_kinds)
                    kinds = [item for item in list(values or []) if item is not None]
                    if not kinds:
                        return []
                    try:
                        return by_kind(kinds)
                    except TypeError:
                        # Older library builds accepted one playlist kind at a
                        # time. Keep compatibility without falling back to the
                        # desktop application.
                        playlists: list[Any] = []
                        for kind in kinds:
                            value = by_kind(kind)
                            if value is None:
                                continue
                            if isinstance(value, (list, tuple)):
                                playlists.extend(value)
                            else:
                                playlists.append(value)
                        return playlists
                raise RuntimeError("У установленной версии yandex-music-api нет метода списка пользовательских плейлистов")

        result = self._retry(fetch, "user playlists")
        if result is None:
            return []
        values = getattr(result, "playlists", None)
        if values is not None:
            return [item for item in list(values) if item is not None]
        try:
            return [item for item in list(result) if item is not None]
        except Exception:
            return []

    def resolve_playlist(self, name: str) -> dict[str, Any] | None:
        requested = str(name or "").strip().strip(" \t\r\n«»\"'")
        if not requested:
            return None
        playlists = self._user_playlists()
        if not playlists:
            return None
        wanted = _norm(requested)
        ranked: list[tuple[float, Any]] = []
        for playlist in playlists:
            title = self._playlist_title(playlist)
            if not title:
                continue
            normalized = _norm(title)
            if normalized == wanted:
                score = 1.0
            elif wanted and wanted in normalized:
                # The request is part of a longer title («Evening» → «Evening Rain»).
                score = 0.80 + 0.08 * (len(wanted) / max(1, len(normalized)))
            elif wanted and normalized and normalized in wanted:
                # MEDIA185-U03: a short title inside a longer request («Day» inside
                # «Night and Day») is weak evidence, never a confident match.
                score = 0.40 + 0.40 * (len(normalized) / max(1, len(wanted)))
            else:
                score = difflib.SequenceMatcher(None, wanted, normalized).ratio()
            ranked.append((float(score), playlist))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked or ranked[0][0] < 0.46:
            return None
        score, playlist = ranked[0]

        def identity(item: Any) -> str:
            return "|".join(str(getattr(item, key, "") or "") for key in ("uid", "owner_uid", "kind", "playlist_uuid", "uuid"))

        rivals = [item for value, item in ranked[1:4] if score - value <= 0.02 and identity(item) != identity(playlist)]
        if rivals:
            # MEDIA185-U13: equally good different playlists are a question for
            # the user, not the API's list order.
            names = [self._playlist_title(item) for item in [playlist, *rivals]]
            raise PlaylistAmbiguous(requested, names)
        return {
            "title": self._playlist_title(playlist),
            "score": round(score, 3),
            "kind": str(getattr(playlist, "kind", "") or ""),
            "uid": str(getattr(playlist, "uid", "") or getattr(playlist, "owner_uid", "") or ""),
            "playlist_uuid": str(getattr(playlist, "playlist_uuid", "") or getattr(playlist, "uuid", "") or ""),
            "object": playlist,
        }

    def playlist_tracks(self, name: str) -> dict[str, Any]:
        resolved = self.resolve_playlist(name)
        if not resolved or resolved.get("object") is None:
            raise RuntimeError(f"Не нашёл плейлист «{name}» в вашей Яндекс Музыке")
        playlist = resolved["object"]
        tracks: list[Any] = []
        fetch = getattr(playlist, "fetch_tracks", None)
        if callable(fetch):
            try:
                tracks = [item for item in list(self._retry(fetch, f"playlist tracks {resolved['title']}") or []) if item is not None]
            except Exception as exc:
                logger.warning("Yandex playlist.fetch_tracks failed for %r: %s", resolved["title"], exc)
        if not tracks:
            raw = getattr(playlist, "tracks", None)
            if raw is not None:
                tracks = [item for item in list(raw) if item is not None]
        if not tracks:
            raise RuntimeError(f"Плейлист «{resolved['title']}» не содержит доступных треков")
        return {**{k: v for k, v in resolved.items() if k != "object"}, "tracks": tracks, "count": len(tracks)}

    @staticmethod
    def _shuffle_token(track: Any) -> str:
        return _track_id(track) or _track_fingerprint(track)

    def _pop_sequence_track(self, values: list[Any]) -> Any:
        # MEDIA185-U07: the queue itself is the (already shuffled) plan. The
        # preview reads its head and Next consumes exactly that head; no second
        # independent random draw can disagree with what was shown.
        self._shuffle_avoid_next_token = ""
        if not values:
            return None
        return values.pop(0)

    @staticmethod
    def _finite_station(station: str) -> bool:
        return str(station or "").startswith(("playlist:", "mixed:", "youtube:"))

    def _cycle_from_seed(self, seed: list[Any], *, avoid_first: str = "") -> list[Any]:
        """A full new repeat cycle in playlist order (or one fresh shuffle)."""
        cycle = list(seed)
        if self._shuffle_enabled and len(cycle) > 1:
            random.SystemRandom().shuffle(cycle)
            if avoid_first and _track_id(cycle[0]) == avoid_first:
                swap = random.SystemRandom().randrange(1, len(cycle))
                cycle[0], cycle[swap] = cycle[swap], cycle[0]
        return cycle

    @_local_source_command
    @_abandon_on_failure
    def play_playlist(self, name: str) -> dict[str, Any]:
        if not self._enabled() or not self._background_enabled():
            raise RuntimeError("Фоновый API-плеер Яндекс Музыки отключён")
        info = self.playlist_tracks(name)
        tracks = list(info.get("tracks") or [])
        playlist_seed = list(tracks)
        with self._playback_lock:
            self._playlist_repeat_seed = list(playlist_seed)
        if self._shuffle_enabled and len(tracks) > 1:
            random.SystemRandom().shuffle(tracks)
            first_raw = tracks.pop(0)
        else:
            first_raw = self._pop_sequence_track(tracks)
        first = self._materialize_track(first_raw)
        if first is None or not hasattr(first, "get_download_info"):
            raise RuntimeError(f"Не удалось получить первый трек плейлиста «{info.get('title', name)}»")
        station = f"playlist:{info.get('kind') or info.get('playlist_uuid') or _norm(str(info.get('title', name)))}"
        data = self._play_track_object(
            first, station=station, batch_id="", queue=tracks,
            auto_advance=True, remember_previous=True,
        )
        logger.info(
            "Yandex playlist started through API-only player: %s (%d tracks) shuffle=%s first=%s preview=%s",
            info.get("title"), int(info.get("count", 0)), bool(self._shuffle_enabled), _track_id(first),
            [self._shuffle_token(item) for item in tracks[:6]],
        )
        return {
            **data, "playlist": str(info.get("title", name)),
            "playlist_kind": str(info.get("kind", "") or ""),
            "queue_length": max(0, len(tracks)), "api_only": True,
        }

    def _wave_history_path(self) -> Path | None:
        try:
            return Path(self.config.paths.root) / "yandex_wave_recent.json"
        except Exception:
            return None

    @wave_synchronized
    def _prune_wave_recent(self) -> None:
        # V7.7: retain a full month so My Wave diversity survives restarts and
        # day-to-day use. IDs and normalized title+artist fingerprints both count.
        cutoff = time.time() - 30.0 * 24.0 * 60.0 * 60.0
        cleaned: list[dict[str, Any]] = []
        for raw in getattr(self, "_wave_recent_entries", []):
            if not isinstance(raw, dict):
                continue
            tid = str(raw.get("id", "") or "").strip()
            fp = str(raw.get("fingerprint", "") or "").strip()
            try:
                ts = float(raw.get("ts", 0.0) or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts <= 0.0:
                ts = time.time()
            if ts < cutoff:
                continue
            if not tid and not fp:
                continue
            cleaned.append({"id": tid, "fingerprint": fp, "ts": ts})
        self._wave_recent_entries = cleaned[-2000:]
        self._wave_recent_ids = [str(x.get("id", "") or "") for x in self._wave_recent_entries if str(x.get("id", "") or "")]
        self._wave_recent_fingerprints = [str(x.get("fingerprint", "") or "") for x in self._wave_recent_entries if str(x.get("fingerprint", "") or "")]

    @wave_synchronized
    def _load_wave_recent_ids(self) -> None:
        path = self._wave_history_path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            now = time.time()
            entries = payload.get("recent", []) if isinstance(payload, dict) else []
            if isinstance(entries, list) and entries:
                self._wave_recent_entries = [dict(x) for x in entries if isinstance(x, dict)]
            else:
                # Backward compatibility with HOTFIX4-6 history. Give old IDs a
                # conservative recent timestamp so an upgrade does not immediately
                # replay the same songs the user just heard.
                values = payload.get("recent_track_ids", []) if isinstance(payload, dict) else []
                self._wave_recent_entries = [
                    {"id": str(x).strip(), "fingerprint": "", "ts": now - 30.0 * 60.0}
                    for x in values if str(x).strip()
                ][-80:]
            self._prune_wave_recent()
        except Exception as exc:
            logger.debug("Yandex My Wave recent-history load failed: %s", exc)

    @wave_synchronized
    def _persist_wave_recent_ids(self) -> None:
        path = self._wave_history_path()
        if path is None:
            return
        # HIST-083: the attempt and its real outcome are recorded separately, so
        # «we tried to save» is never mistaken for «the history is on disk».
        self.__dict__["_wave_persist188"] = {"attempted_at": time.time(), "saved": None, "error": ""}
        try:
            disk = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            self._wave_recent_entries = merge_entries(disk.get("recent", []) if isinstance(disk, dict) else [], self._wave_recent_entries)
            self._prune_wave_recent()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({
                    "recent": self._wave_recent_entries[-2000:],
                    "recent_track_ids": self._wave_recent_ids[-2000:],
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            self._wave_persist188.update(saved=True)
        except Exception as exc:
            self._wave_persist188.update(saved=False, error=f"{type(exc).__name__}: {str(exc)[:160]}")
            logger.debug("Yandex My Wave recent-history save failed: %s", exc)

    @wave_synchronized
    def _remember_wave_track(self, track: Any) -> None:
        tid = _track_id(track)
        fp = _track_fingerprint(track)
        if not tid and not fp:
            return
        now = time.time()
        # Remove old entries referring to the same API object *or* the same
        # title/artist recording, then append the new observation.
        self._wave_recent_entries = [
            x for x in self._wave_recent_entries
            if not ((tid and str(x.get("id", "") or "") == tid) or (fp and str(x.get("fingerprint", "") or "") == fp))
        ]
        self._wave_recent_entries.append({"id": tid, "fingerprint": fp, "ts": now})
        self._prune_wave_recent()
        self._persist_wave_recent_ids()

    @wave_synchronized
    def _remember_wave_track_id(self, track_id: str) -> None:
        # Compatibility helper for older call sites/tests. New playback paths call
        # _remember_wave_track() so title/artist duplicates are suppressed too.
        tid = str(track_id or "").strip()
        if not tid:
            return
        self._wave_recent_entries = [x for x in self._wave_recent_entries if str(x.get("id", "") or "") != tid]
        self._wave_recent_entries.append({"id": tid, "fingerprint": "", "ts": time.time()})
        self._prune_wave_recent()
        self._persist_wave_recent_ids()

    def remember_wave_track_id(self, track_id: str) -> None:
        """Persist a My Wave track played by another JARVIS transport."""
        self._remember_wave_track_id(track_id)

    def remember_wave_track(self, track: Any) -> None:
        """Persist ID plus title/artist fingerprint for cross-transport anti-repeat."""
        self._remember_wave_track(track)

    @wave_synchronized
    def _wave_recent_sets(self, *, max_age_hours: float | None = None, max_items: int = 0) -> tuple[set[str], set[str]]:
        history_path = self._wave_history_path()
        if history_path is not None and history_path.is_file():
            try:
                disk = json.loads(history_path.read_text(encoding="utf-8"))
                self._wave_recent_entries = merge_entries(disk.get("recent", []) if isinstance(disk, dict) else [], getattr(self, "_wave_recent_entries", []))
            except (OSError, ValueError):
                pass  # In-memory history remains authoritative if disk is unavailable.
        self._prune_wave_recent()
        values = list(self._wave_recent_entries)
        if max_age_hours is not None:
            cutoff = time.time() - max(0.0, float(max_age_hours)) * 60.0 * 60.0
            values = [x for x in values if float(x.get("ts", 0.0) or 0.0) >= cutoff]
        if int(max_items or 0) > 0:
            values = values[-int(max_items):]
        ids = {str(x.get("id", "") or "").strip() for x in values if str(x.get("id", "") or "").strip()}
        fingerprints = {str(x.get("fingerprint", "") or "").strip() for x in values if str(x.get("fingerprint", "") or "").strip()}
        return ids, fingerprints

    def _wave_repeat_floor(self) -> tuple[set[str], set[str]]:
        """Configured recency preference; strict/custom settings remain mandatory."""
        config = getattr(self, "config", None)
        get = getattr(config, "get", lambda key, default: default)
        try:
            hours = max(1, min(168, float(get("integrations.yandex_music.wave_no_repeat_hours", 12))))
            count = max(10, min(500, int(get("integrations.yandex_music.wave_no_repeat_tracks", 80))))
        except (TypeError, ValueError):
            hours, count = 12, 80
        ids, fingerprints = self._wave_recent_sets(max_age_hours=hours)
        recent_ids, recent_fp = self._wave_recent_sets(max_items=count)
        # The shipped 12h/80 defaults must not prohibit the last 80 songs
        # forever. Bound that default history by BOTH age and count. A custom
        # user window, or an explicit strict policy, retains the old union.
        policy = str(get("integrations.yandex_music.wave_repeat_policy", "bounded_default"))
        if policy == "strict" or (hours, count) != (12, 80):
            return ids | recent_ids, fingerprints | recent_fp
        return ids & recent_ids, fingerprints & recent_fp

    def wave_strict_repeat_policy(self) -> bool:
        """Respect an explicit strict policy or custom anti-repeat window."""
        get = getattr(getattr(self, "config", None), "get", lambda key, default: default)
        try:
            return (str(get("integrations.yandex_music.wave_repeat_policy", "bounded_default")) == "strict"
                    or float(get("integrations.yandex_music.wave_no_repeat_hours", 12)) != 12
                    or int(get("integrations.yandex_music.wave_no_repeat_tracks", 80)) != 80)
        except (TypeError, ValueError):
            return True

    def _wave_playback_floor(self) -> tuple[set[str], set[str]]:
        """Default history is a preference; never replay the just-played recording."""
        if self.wave_strict_repeat_policy():
            return self._wave_repeat_floor()
        self._wave_recent_sets()  # merge cross-transport durable history first
        with self._wave_history_lock:
            last = (getattr(self, "_wave_recent_entries", []) or [{}])[-1]
            return ({str(last["id"])} if last.get("id") else set(),
                    {str(last["fingerprint"])} if last.get("fingerprint") else set())

    def _batch_for_track(self, track: Any, station: str, fallback: str = "") -> str:
        if isinstance(track, dict) and track.get("_rotor_station") == station:
            return str(track.get("_rotor_batch_id") or "")
        with getattr(self, "_track_batch_lock", self._playback_lock):
            item = getattr(self, "_track_batches", {}).get(id(track))
        if item and item[0] is track and item[1] == station:
            return item[2]
        return str(fallback or "")

    def wave_tracks(self, *, queue_track_id: str = "", avoid_ids: Any = None, fresh_start: bool = False, fast_start: bool = False, target_count: int = 0) -> dict[str, Any]:
        """Read a real Rotor continuation, preserving policy and feedback order.

        API batches can be shorter than the requested preview. Return only real
        candidates; do not pretend future IDs are pagination cursors or report
        listening events for unplayed tracks to manufacture a larger queue.
        """
        client = self._api_client()
        if client is None:
            raise RuntimeError(self._api_unavailable_message("персональной станции «Моя волна»"))
        station = 'user:onyourwave'
        floor_ids, floor_fp = self._wave_repeat_floor()
        avoid = {str(x) for x in (avoid_ids or []) if str(x).strip()}
        if queue_track_id: avoid.add(str(queue_track_id))
        floor_ids = set(floor_ids) | avoid
        hard_ids, hard_fp = self._wave_playback_floor()
        hard_ids = set(hard_ids) | avoid
        old_ids, old_fp = self._wave_recent_sets(max_age_hours=7 * 24)
        if fresh_start:
            self._begin_wave_session(client)
        # Rotor's queue is an actually played track, never an unplayed queue tail.
        # A fresh start can return the SAME five songs from the previous run.
        # In that case resume once from genuine playback history instead of
        # blacklisting that entire batch and repeatedly querying queue="".
        hint = "" if fresh_start else str(queue_track_id or "")
        recovery_hint = str(queue_track_id or "")
        if not recovery_hint and fresh_start:
            with self._wave_history_lock:
                recovery_hint = next((str(x.get("id") or "") for x in
                    reversed(getattr(self, "_wave_recent_entries", [])) if x.get("id")), "")
        tried = set()
        seen_ids, seen_fp = set(), set()
        primary, continuity, older_candidates = [], [], []
        merged_count = repeated = unavailable = 0
        errors = []
        batch_id = ''
        self._wait_wave_feedback()
        for request_hint in dict.fromkeys([hint, recovery_hint] if fresh_start and recovery_hint else [hint]):
            tried.add(request_hint)
            try:
                tracks, batch_id = self._station_tracks(station, queue_track_id=request_hint)
            except Exception as exc:
                errors.append(type(exc).__name__)
                logger.warning('My Wave batch request failed: %s', type(exc).__name__)
                if self._yandex_error_kind(exc) == "network":
                    self._api_last_error_kind = "network"
                break
            merged_count += len(tracks)
            for track in tracks:
                tid, fp = _track_id(track), _track_fingerprint(track)
                available = track.get('available', True) if isinstance(track, dict) else getattr(track, 'available', True)
                if not tid or available is False:
                    unavailable += 1
                    continue
                if tid in seen_ids or (fp and fp in seen_fp):
                    continue
                seen_ids.add(tid)
                if fp:
                    seen_fp.add(fp)
                if tid in floor_ids or (fp and fp in floor_fp):
                    repeated += 1
                    if not self.wave_strict_repeat_policy() and tid not in hard_ids and not (fp and fp in hard_fp):
                        older_candidates.append(track)
                    continue
                if tid in old_ids or (fp and fp in old_fp):
                    continuity.append(track)
                else:
                    primary.append(track)
            if primary or continuity:
                break
        unique = primary + continuity
        historical_fallback = 0
        if not unique and older_candidates and not errors:
            with self._wave_history_lock:
                history = list(getattr(self, "_wave_recent_entries", []))
            ages = {str(x.get("id") or ""): float(x.get("ts") or 0) for x in history}
            unique = sorted(older_candidates, key=lambda track: ages.get(_track_id(track), 0))
            historical_fallback = len(unique)
        if unique:
            batch_id = self._batch_for_track(unique[0], station, batch_id)
        reason = 'ready' if unique else 'recent_tracks_filtered' if merged_count and repeated else 'no_playable_tracks' if merged_count else 'api_error_or_empty'
        diagnostic = {'station': station, 'reason': reason, 'probe_count': len(tried),
            'received_candidates': merged_count, 'recent_filtered': repeated,
            'unavailable_filtered': unavailable, 'accepted': len(unique), 'errors': errors,
            'time': time.time(), 'repeat_floor_enforced': not bool(historical_fallback),
            'immediate_repeat_guard_enforced': True,
            'history_continuation_used': bool(recovery_hint and recovery_hint in tried and recovery_hint != hint),
            'historical_fallback_count': historical_fallback,
            'history_policy': 'strict' if self.wave_strict_repeat_policy() else 'prefer_fresh'}
        self._last_wave_diagnostic = diagnostic
        try:
            root = getattr(getattr(self.config, 'paths', None), 'root', None)
            if root:
                from jarvis.runtime.diagnostics import write_report
                write_report(Path(root) / 'diagnostics' / 'wave_last.json', diagnostic)
        except (OSError, ValueError):
            logger.debug('Could not save Wave diagnostics', exc_info=True)
        logger.info('My Wave result: %s', diagnostic)
        return {**diagnostic, 'batch_id': batch_id, 'tracks': unique, 'count': len(unique),
            'fresh_count': len(primary), 'soft_fallback_count': historical_fallback, 'tail_fallback_count': 0,
            'network_unavailable': bool(errors and not unique and self._api_last_error_kind == "network"),
            'continuity_fallback_count': len(continuity), 'diversity_v56': True,
            'continuity_v132': True, 'fast_start': bool(fast_start)}

    def _begin_wave_session(self, client) -> None:
        """A real user requested a station start; do not fabricate listening events."""
        start = getattr(client, 'rotor_station_feedback_radio_started', None)
        if not callable(start): return
        try:
            with self._api_lock:
                start('user:onyourwave', from_='neo-jarvis-background', timeout=2.5)
        except Exception as exc:
            logger.debug('Rotor session start unconfirmed: %s', type(exc).__name__)

    @_local_source_command
    @_abandon_on_failure
    def play_my_tracks(self) -> dict[str, Any]:
        """Play the user's liked tracks entirely through the background API/player."""
        if not self._enabled():
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        if not self._background_enabled():
            raise RuntimeError("Фоновый плеер Яндекс Музыки отключён")
        likes = self._liked_tracks()
        if not likes:
            raise RuntimeError("В «Моих треках» Яндекс Музыки не найдено доступных композиций")
        # Keep lightweight TrackShort objects in the queue; the next item is
        # materialized only when it is about to play, avoiding hundreds of API
        # detail requests at startup.
        remaining_likes = list(likes)
        if self._shuffle_enabled and len(remaining_likes) > 1:
            random.SystemRandom().shuffle(remaining_likes)
            first_raw = remaining_likes.pop(0)
        else:
            first_raw = self._pop_sequence_track(remaining_likes)
        first = self._materialize_track(first_raw)
        if first is None or not hasattr(first, "get_download_info"):
            raise RuntimeError("Не удалось получить первый трек из «Моих треков» через API")
        data = self._play_track_object(
            first, station="likes:library", batch_id="", queue=remaining_likes,
            auto_advance=True, remember_previous=True,
        )
        logger.info(
            "Yandex My Tracks started through API-only liked-tracks queue: %d items shuffle=%s first=%s preview=%s",
            len(likes), bool(self._shuffle_enabled), _track_id(first),
            [self._shuffle_token(item) for item in remaining_likes[:6]],
        )
        return {**data, "library": "likes", "queue_length": max(0, len(remaining_likes)), "shuffle": bool(self._shuffle_enabled), "api_only": True}

    def _native_player_toggle(self, kind: str, enabled: bool) -> dict[str, Any]:
        launch_yandex_music(wait_seconds=20.0)
        window = focus_yandex_music_window()
        if window is None:
            raise RuntimeError("Не вижу окно приложения Яндекс Музыка")
        if kind == "shuffle":
            on_state = ("выключить перемешивание", "shuffle on", "disable shuffle")
            off_state = ("включить перемешивание", "shuffle off", "enable shuffle", "перемешать")
        else:
            on_state = ("выключить повтор", "repeat on", "disable repeat", "повтор включен")
            off_state = ("включить повтор", "repeat off", "enable repeat", "повтор")
        already = self._find_controls(window, on_state if enabled else off_state)
        # If the button label describes the action that would reverse the desired
        # state, the requested state is already active.
        if already:
            return {"app": "yandex_music", kind: enabled, "verified": True, "already_in_requested_state": True, "method": f"native_uia_{kind}_state"}
        candidates = self._find_controls(window, off_state if enabled else on_state)
        for control in candidates[:10]:
            if self._invoke(control):
                time.sleep(0.25)
                return {"app": "yandex_music", kind: enabled, "verified": True, "method": f"native_uia_{kind}"}
        raise RuntimeError(f"Яндекс Музыка не предоставила доступную кнопку {'перемешивания' if kind == 'shuffle' else 'повтора'}")

    def set_shuffle(self, enabled: bool) -> dict[str, Any]:
        if not self._enabled() and not self._is_youtube_entry(self._current_track_obj):
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        value = bool(enabled)
        randomized = False
        preview_ids: list[str] = []
        with self._playback_lock:
            self._shuffle_enabled = value
            self._shuffle_avoid_next_token = ""
            # HOTFIX40.2: mutate the *live* pending queue, not merely the stored
            # preference. Preserve the current song, but reshuffle everything
            # after it. Also remember the old sequential next item so the first
            # post-toggle advance is guaranteed to jump elsewhere when possible.
            if value and len(self._station_queue) > 1:
                old_next = self._shuffle_token(self._station_queue[0])
                random.SystemRandom().shuffle(self._station_queue)
                if old_next and self._shuffle_token(self._station_queue[0]) == old_next and len(self._station_queue) > 1:
                    swap_index = random.SystemRandom().randrange(1, len(self._station_queue))
                    self._station_queue[0], self._station_queue[swap_index] = self._station_queue[swap_index], self._station_queue[0]
                self._shuffle_avoid_next_token = old_next
                randomized = True
            queue_length = len(self._station_queue)
            preview_ids = [self._shuffle_token(item) for item in self._station_queue[:6] if self._shuffle_token(item)]
            self._bump_queue_revision()
        self._publish_queue_update()
        # Persist the desired state without touching the foreground Yandex app.
        try:
            integrations = self.config.data.setdefault("integrations", {})
            integrations.setdefault("yandex_music", {})["shuffle_enabled"] = value
            self.config.save(self.config.data)
        except Exception as exc:
            logger.debug("Yandex shuffle state persistence failed: %s", exc)
        if value:
            with self._playback_lock:
                next_prefetch_window = list(self._station_queue[:8])
            self._start_prefetch_window(next_prefetch_window, limit=8)
        logger.info(
            "Yandex shuffle %s; pending=%d randomized=%s preview=%s",
            "enabled" if value else "disabled", queue_length, randomized, preview_ids,
        )
        return {
            "app": "yandex_music", "shuffle": value, "queue_length": queue_length,
            "queue_randomized": randomized,
            "verified": True, "method": "jarvis_background_queue_shuffle_bag", "non_invasive": True,
        }

    def set_repeat(self, mode: str | bool = "off") -> dict[str, Any]:
        """Set repeat policy for the hidden JARVIS background queue.

        No desktop/GSMTC/UI fallback is used: repeat belongs to the same in-memory
        queue that actually drives Yandex playback, so the setting is deterministic
        and never steals keyboard/mouse focus.
        """
        if not self._enabled() and not self._is_youtube_entry(self._current_track_obj):
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        if isinstance(mode, bool):
            requested = "track" if mode else "off"
        else:
            requested = str(mode or "off").strip().casefold()
        requested = {
            "on": "track", "one": "track", "song": "track",
            "context": "playlist", "queue": "playlist",
            "none": "off", "false": "off",
        }.get(requested, requested)
        if requested not in {"off", "track", "playlist"}:
            raise ValueError("Режим повтора должен быть: off, track или playlist")
        arm_monitor = False
        with self._playback_lock:
            self._repeat_mode = requested
            queue_length = len(self._station_queue)
            if requested != "off" and self._current_track_obj is not None and not self._auto_advance:
                # MEDIA185-U09: repeat needs end-of-track monitoring even for
                # one loaded item. Turning it on never resumes a paused player.
                self._auto_advance = True
                if not self._station:
                    self._station = ("mixed:youtube" if self._is_youtube_entry(self._current_track_obj)
                                     else f"track:{_track_id(self._current_track_obj)}")
                if requested == "playlist" and not self._playlist_repeat_seed:
                    self._playlist_repeat_seed = [self._current_track_obj, *self._station_queue]
                arm_monitor = True
        if arm_monitor:
            self._start_station_monitor()
        try:
            integrations = self.config.data.setdefault("integrations", {})
            integrations.setdefault("yandex_music", {})["repeat_mode"] = requested
            self.config.save(self.config.data)
        except Exception as exc:
            logger.debug("Yandex repeat state persistence failed: %s", exc)
        return {
            "app": "yandex_music", "repeat_mode": requested,
            "repeat_track": requested == "track", "repeat_playlist": requested == "playlist",
            "queue_length": queue_length, "verified": True,
            "method": "jarvis_background_queue_repeat_mode", "non_invasive": True,
        }

    @staticmethod
    def _quality(info: Any) -> tuple[int, int]:
        codec = str(getattr(info, "codec", "") or "").casefold()
        codec_rank = {"mp3": 6, "aac": 5, "flac": 4, "opus": 3, "ogg": 2}.get(codec, 1)
        bitrate = int(getattr(info, "bitrate_in_kbps", 0) or 0)
        return codec_rank, bitrate

    @staticmethod
    def _retry(callable_obj: Any, label: str, *, attempts: int = 3, delay: float = 0.35) -> Any:
        last: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return callable_obj()
            except Exception as exc:
                last = exc
                logger.warning("Yandex %s attempt %s/%s failed: %s", label, attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(delay * attempt)
        if last is not None:
            raise last
        return None

    def resolve_track(self, query: str, artist: str = "") -> dict[str, Any] | None:
        original_query = str(query or "").strip()
        query = _clean_spoken_track_query(original_query)
        artist = str(artist or "").strip()
        if not query:
            return None
        client = self._api_client()
        if client is None:
            return None

        # A Discord /play request may contain a direct Yandex Music URL. Resolve
        # the stable track id instead of feeding the whole URL into text search.
        url_track = re.search(r"(?:music\.yandex\.[^/]+|music\.yandex\.ru)/(?:album/\d+/)?track/(\d+)", original_query, re.I)
        if url_track is None:
            url_track = re.search(r"/track/(\d+)", original_query, re.I)
        if url_track is not None:
            track_id = str(url_track.group(1) or "").strip()
            try:
                def fetch_direct() -> Any:
                    with self._api_lock:
                        return client.tracks([track_id])
                direct = self._retry(fetch_direct, "direct track lookup")
                direct_tracks = list(direct or [])
                track = direct_tracks[0] if direct_tracks else None
                if track is not None and _track_id(track):
                    return {
                        "id": _track_id(track),
                        "title": _track_title(track),
                        "artist": _artists_text(track),
                        "score": 1.0,
                        "object": track,
                    }
            except Exception as exc:
                logger.info("Yandex direct track URL lookup failed, falling back to search: %s", exc)

        search_text = " ".join(x for x in (artist, query) if x).strip()

        def do_search() -> Any:
            with self._api_lock:
                try:
                    return client.search(search_text, type_="track")
                except TypeError:
                    return client.search(search_text, type="track")

        try:
            result = self._retry(do_search, "track search")
        except Exception as exc:
            logger.warning("Yandex API track search failed after retries: %s", exc)
            return None
        tracks = _flatten_search_tracks(result)
        if not tracks:
            return None

        # Yandex's own `best` track is authoritative enough to avoid a second
        # local fuzzy gate rejecting phonetic ASR such as "по парации Lady Gaga".
        # We still report a local score for diagnostics; if no best track exists,
        # use conservative local ranking as before.
        best_container = getattr(result, "best", None)
        best_type = str(getattr(best_container, "type", "") or "").casefold()
        best_result = getattr(best_container, "result", None) if best_container is not None else None
        if best_result is not None and hasattr(best_result, "title") and _track_id(best_result):
            score = _similarity(query, artist, best_result)
            logger.info(
                "Yandex track search accepted provider best: query=%r cleaned=%r -> %s — %s (local_score=%.3f)",
                original_query, query, _artists_text(best_result), _track_title(best_result), score,
            )
            return {
                "id": _track_id(best_result),
                "title": _track_title(best_result),
                "artist": _artists_text(best_result),
                "score": round(float(score), 3),
                "object": best_result,
            }

        ranked = sorted(
            ((_similarity(query, artist, track), track) for track in tracks[:35]),
            key=lambda item: item[0], reverse=True,
        )
        if not ranked or ranked[0][0] < 0.42:
            return None
        score, track = ranked[0]
        return {
            "id": _track_id(track),
            "title": _track_title(track),
            "artist": _artists_text(track),
            "score": round(float(score), 3),
            "object": track,
        }

    def _stream_candidates_for_track(self, track: Any, meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        meta = dict(meta or {})
        track_key = _track_id(track)
        prepared = self._prepared_candidates.get(track_key)
        if prepared:
            ready = [dict(c) for c in prepared[1] if self._cached_candidate_path(c)]
            if ready:
                return ready
            if time.monotonic() - prepared[0] < 180:
                return [dict(c) for c in prepared[1]]

        def get_infos() -> list[Any]:
            with self._api_lock:
                return list(track.get_download_info() or [])

        try:
            infos = self._retry(get_infos, "download-info")
        except Exception as exc:
            logger.warning("Yandex API download-info failed after retries: %s", exc)
            if self._yandex_error_kind(exc) == "network":
                raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.") from exc
            return []
        candidates: list[dict[str, Any]] = []
        for info in sorted(infos or [], key=self._quality, reverse=True):
            if bool(getattr(info, "preview", False)):
                continue
            try:
                url = str(self._retry(lambda info=info: info.get_direct_link(), "direct-link", attempts=2, delay=0.2) or "").strip()
            except Exception as exc:
                if self._yandex_error_kind(exc) == "network":
                    raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.") from exc
                continue
            if not url:
                continue
            candidates.append({
                **{k: v for k, v in meta.items() if k != "object"},
                "id": str(meta.get("id", "") or _track_id(track)),
                "title": str(meta.get("title", "") or _track_title(track)),
                "artist": str(meta.get("artist", "") or _artists_text(track)),
                "url": url,
                "codec": str(getattr(info, "codec", "") or ""),
                "bitrate_kbps": int(getattr(info, "bitrate_in_kbps", 0) or 0),
                "expected_duration_ms": int(getattr(track, "duration_ms", 0) or 0),
                "object": track,
            })
        if candidates:
            self._prepared_candidates[track_key] = (time.monotonic(), [dict(c) for c in candidates])
            while len(self._prepared_candidates) > 32:
                self._prepared_candidates.pop(next(iter(self._prepared_candidates)))
        return candidates

    def resolve_stream_candidates(self, query: str, artist: str = "") -> list[dict[str, Any]]:
        # External players (most importantly the Discord bot) used to call a
        # single resolve_track(), while the local Yandex player had richer spoken
        # retry logic. Use the same candidates everywhere so a phrase recognized as
        # "dula marginstern" has the same chance of resolving in Discord as locally.
        attempted: set[str] = set()
        for candidate in (_spoken_query_candidates(query) or [_clean_spoken_track_query(query) or str(query or "").strip()]):
            key = _norm(candidate)
            if not candidate or key in attempted:
                continue
            attempted.add(key)
            resolved = self.resolve_track(candidate, artist)
            if resolved and resolved.get("object") is not None:
                return self._stream_candidates_for_track(resolved["object"], resolved)
        return []

    def resolve_stream(self, query: str, artist: str = "") -> dict[str, Any] | None:
        """Resolve a catalog track and obtain a short-lived direct audio URL."""
        candidates = self.resolve_stream_candidates(query, artist)
        return candidates[0] if candidates else None

    def resolve_playback_candidates(self, query: str, artist: str = "", *, cache_first: bool = True) -> list[dict[str, Any]]:
        """Resolve sources suitable for a long-lived player such as Discord FFmpeg.

        The first candidate is downloaded completely when possible so playback is
        not tied to a short-lived CDN URL. Remaining candidates stay as direct
        fallbacks and are only used if the preferred source fails.
        """
        candidates = self.resolve_stream_candidates(query, artist)
        prepared: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            item = dict(candidate)
            item["source"] = str(item.get("url", "") or "")
            item["source_method"] = "direct_stream"
            if cache_first and index == 0:
                try:
                    item["source"] = self._cache_candidate(item)
                    item["source_method"] = "cached_local_file"
                except Exception as exc:
                    logger.warning("Yandex source cache for external player failed; direct URL will be used: %s", exc)
            prepared.append(item)
        return prepared

    def prepare_playback_candidates_for_track(
        self, track: Any, *, title: str = "", artist: str = "", cache_first: bool = False,
        wait_for_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """Prepare Discord/player sources from an already resolved Yandex track.

        Queue entries keep the resolved Track object so skip/auto-advance does not
        search the spoken title a second time. This also prevents ASR spelling from
        turning a previously valid queued item into a misleading OAuth error.
        """
        try:
            track = self._materialize_track(track)
        except Exception as exc:
            logger.warning("Yandex queued track materialization failed: %s", exc)
            return []
        if track is None or not hasattr(track, "get_download_info"):
            return []
        meta = {
            "id": _track_id(track),
            "title": str(title or _track_title(track)),
            "artist": str(artist or _artists_text(track)),
            "object": track,
        }
        candidates = self._stream_candidates_for_track(track, meta)
        prepared: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            item = dict(candidate)
            item["source"] = str(item.get("url", "") or "")
            item["source_method"] = "direct_stream"
            if cache_first and index == 0:
                try:
                    cached = self._cached_candidate_path(item)
                    if cached:
                        item["source"] = cached
                        item["source_method"] = "cached_local_file_ready"
                    elif wait_for_cache:
                        item["source"] = self._cache_candidate(item)
                        item["source_method"] = "cached_local_file"
                except Exception as exc:
                    logger.warning("Yandex queued source cache failed; direct URL will be used: %s", exc)
            prepared.append(item)
        return prepared

    def _prefetch_track(self, track: Any) -> None:
        if self._is_youtube_entry(track):
            source_url = normalize_youtube_url(str(track.get("_source_url", "") or track.get("query", "") or ""))
            if not source_url:
                return
            try:
                prepared = self._youtube.download_audio(source_url)
                path = str(prepared.get("local_path", "") or "").strip()
                if path and os.path.isfile(path):
                    track["_youtube_cached_path"] = path
                    track["_youtube_cached_meta"] = dict(prepared)
                    logger.debug("Local YouTube prefetch ready for %r", track.get("title") or source_url)
            except Exception as exc:
                logger.debug("Local YouTube prefetch failed: %s", exc)
            return
        # Liked-tracks API returns TrackShort. Materialize only the item that is
        # actually being prefetched; otherwise get_download_info() does not exist.
        try:
            track = self._materialize_track(track)
        except Exception as exc:
            logger.debug("Yandex next-track materialization failed: %s", exc)
            return
        track_id = _track_id(track)
        if not track_id or not hasattr(track, "get_download_info"):
            return
        with self._prefetch_lock:
            if track_id in self._prefetching_ids:
                return
            self._prefetching_ids.add(track_id)
        try:
            meta = {"id": track_id, "title": _track_title(track), "artist": _artists_text(track), "object": track}
            pref_candidates = self._stream_candidates_for_track(track, meta)
            # A lower-bitrate full file finishes sooner and is ideal for the
            # latency-critical skip path. Prefer the highest candidate <=192 kbps
            # when available, then fall back to the normal quality ordering.
            responsive = [c for c in pref_candidates if 0 < int(c.get("bitrate_kbps", 0) or 0) <= 192]
            ordered = sorted(responsive, key=lambda c: int(c.get("bitrate_kbps", 0) or 0), reverse=True)
            seen = {id(c) for c in ordered}
            ordered.extend(c for c in pref_candidates if id(c) not in seen)
            for candidate in ordered[:3]:
                try:
                    self._cache_candidate(candidate)
                    logger.debug("Yandex prefetched next track %s bitrate=%s", track_id, candidate.get("bitrate_kbps"))
                    return
                except Exception as exc:
                    logger.debug("Yandex prefetch candidate failed for %s: %s", track_id, exc)
        except Exception as exc:
            logger.debug("Yandex next-track prefetch failed for %s: %s", track_id, exc)
        finally:
            with self._prefetch_lock:
                self._prefetching_ids.discard(track_id)

    def _start_prefetch(self, track: Any | None) -> None:
        if track is None or not _track_id(track):
            return
        PREFETCH.submit(f"yandex:{_track_id(track)}", lambda: self._prefetch_track(track))

    def _start_prefetch_window(self, tracks: list[Any], *, limit: int = 8) -> None:
        # Keep a warm 5-10 track window without accumulating worker threads during
        # a long session. There is exactly one window worker; every queue mutation
        # replaces its desired snapshot and bumps a generation counter.
        items = [
            track for track in list(tracks or [])[:max(0, min(10, int(limit)))]
            if track is not None and _track_id(track)
        ]
        if not items:
            return
        should_start = False
        token = object()
        with self._prefetch_lock:
            self._prefetch_window_generation += 1
            self._prefetch_window_items = list(items)
            thread = self._prefetch_window_thread
            # REC-PREFETCH-WINDOW-START-OWNERSHIP-01: a published-but-not-yet-
            # started worker is already the owner. "Not alive" alone is not
            # "nobody accepted the start", so one reservation guards it.
            if getattr(self, "_prefetch_window_starting", None) is None and (thread is None or not thread.is_alive()):
                self._prefetch_window_starting = token
                should_start = True
        self._prefetch_window_event.set()
        if not should_start:
            return

        def worker() -> None:
            while True:
                self._prefetch_window_event.wait()
                self._prefetch_window_event.clear()
                with self._prefetch_lock:
                    generation = self._prefetch_window_generation
                    batch = list(self._prefetch_window_items)
                for item in batch:
                    with self._prefetch_lock:
                        if generation != self._prefetch_window_generation:
                            break
                    self._prefetch_track(item)

        thread = threading.Thread(target=worker, name="neo-yandex-prefetch-window", daemon=True)
        with self._prefetch_lock:
            if self._prefetch_window_starting is not token:
                return
            self._prefetch_window_thread = thread
        try:
            thread.start()
        except BaseException:
            # A failed start releases only its own reservation and never a
            # newer owner, generation or batch.
            with self._prefetch_lock:
                if self._prefetch_window_starting is token:
                    self._prefetch_window_starting = None
                    if self._prefetch_window_thread is thread:
                        self._prefetch_window_thread = None
            raise
        with self._prefetch_lock:
            if self._prefetch_window_starting is token:
                self._prefetch_window_starting = None

    def _ensure_wave_queue_depth(self, minimum: int = 8) -> None:
        minimum = max(5, min(10, int(minimum or 8)))
        with self._playback_lock:
            if self._station != "user:onyourwave" or len(self._station_queue) >= minimum:
                return
        with self._wave_refill_lock:
            if self._wave_refill_active or time.monotonic() < getattr(self, "_wave_refill_retry_at", 0.0):
                return
            self._wave_refill_active = True

        def worker() -> None:
            try:
                if not self._wave_feedback_ready.wait(3.0):
                    return
                with self._playback_lock:
                    if self._station != "user:onyourwave":
                        return
                    current_id = _track_id(self._current_track_obj) if self._current_track_obj is not None else str(self._current_track_meta.get("id", "") or "")
                    generation = getattr(self, "_wave_queue_generation", 0)
                    existing = list(self._station_queue)
                    avoid = {_track_id(x) for x in existing if _track_id(x)}
                    if self.wave_strict_repeat_policy():
                        avoid.update(_track_id(x) for x in self._history[-20:] if _track_id(x))
                # Documented Rotor continuation uses the previous finished or
                # skipped song, never an unplayed queue tail.
                previous_id = str(self._wave_last_completed_id or "")
                wave = self.wave_tracks(queue_track_id=previous_id, avoid_ids=avoid | {current_id}, fresh_start=False, fast_start=False)
                candidates = list(wave.get("tracks") or [])
                if wave.get("network_unavailable") and not candidates:
                    self._wave_refill_retry_at = time.monotonic() + 20.0
                    return
                if not candidates:
                    self._wave_refill_retry_at = time.monotonic() + 20.0
                    return
                with self._playback_lock:
                    if self._station != "user:onyourwave" or getattr(self, "_wave_queue_generation", 0) != generation:
                        return
                    floor_ids, floor_fp = self._wave_playback_floor()
                    live_current_id = _track_id(self._current_track_obj)
                    # The current track's batch must not be overwritten by a
                    # prefetched, unrelated branch. Each queued track owns its batch.
                    seen = {_track_id(x) for x in self._station_queue if _track_id(x)}
                    if live_current_id:
                        seen.add(live_current_id)
                    seen_fp = {_track_fingerprint(x) for x in [self._current_track_obj, *self._station_queue] if x is not None}
                    for item in candidates:
                        tid, fp = _track_id(item), _track_fingerprint(item)
                        if tid and tid not in seen and tid not in floor_ids and (not fp or (fp not in seen_fp and fp not in floor_fp)):
                            self._station_queue.append(item)
                            seen.add(tid)
                            if fp: seen_fp.add(fp)
                        if len(self._station_queue) >= max(minimum, 12):
                            break
                    warm = list(self._station_queue[:minimum])
                    reported_count = getattr(self, "_queue_reported_count", -1)
                    announce_queue = bool(warm and reported_count >= 0 and min(5, len(warm)) > reported_count
                        and getattr(self, "_queue_reported_track", "") == live_current_id)
                    if announce_queue:
                        self._queue_reported_empty = False
                        self._queue_reported_count = min(5, len(warm))
                    current_meta = dict(self._current_track_meta)
                    playback_generation = self._playback_generation
                self._wave_refill_retry_at = time.monotonic() + (5.0 if warm else 20.0)
                self._after_playback_effect("queue_session_save", self._persist_playback)
                if announce_queue:
                    self._after_playback_effect("queue_ready_notice", publish_now_playing,
                        "yandex_music", str(current_meta.get("title", "")), str(current_meta.get("artist", "")),
                        live_current_id, reason="queue_updated", playing=not self._manual_pause_active,
                        playback_generation=playback_generation)
                self._start_prefetch_window(warm, limit=minimum)
            except Exception as exc:
                self._wave_refill_retry_at = time.monotonic() + 20.0
                logger.debug("Background My Wave queue refill failed: %s", exc)
            finally:
                with self._wave_refill_lock:
                    self._wave_refill_active = False
        threading.Thread(target=worker, name="neo-yandex-wave-refill", daemon=True).start()

    def prefetch_track_for_external(self, track: Any | None) -> None:
        """Warm the Yandex cache for Discord without blocking its voice transport."""
        self._start_prefetch(track)

    def _remember_current(self) -> None:
        if self._current_track_obj is not None:
            stable = self._stable_track(self._current_track_obj, self._station)
            stable["played_at"] = float(self._current_track_meta.get("played_at") or time.time())
            if stable.get("_source_type") == "yandex_saved":
                stable["_live_track"] = self._current_track_obj
            self._history.append(stable)
            self._history = self._history[-5:]

    def _stable_track(self, track: Any, source: str = "") -> dict[str, Any]:
        if isinstance(track, dict) and track.get("_source_type") == "youtube":
            return {key: track[key] for key in (
                "_source_type", "_source_url", "query", "id", "track_id", "title", "artist", "_expected_duration_ms"
            ) if key in track}
        # A prefetched/history entry owns its Rotor origin independently of the
        # currently playing station. Persist that origin even before it is played.
        origin_station = str(track.get("_rotor_station") or "") if isinstance(track, dict) else ""
        origin_batch = str(track.get("_rotor_batch_id") or "") if isinstance(track, dict) else ""
        with getattr(self, "_track_batch_lock", self._playback_lock):
            origin = getattr(self, "_track_batches", {}).get(id(track))
        if origin and origin[0] is track:
            origin_station, origin_batch = origin[1], origin[2]
        if not origin_station:
            origin_station = source
            origin_batch = self._batch_for_track(track, source)
        return {"_source_type": "yandex_saved", "provider": "yandex_music", "id": _track_id(track),
                "title": _track_title(track), "artist": _artists_text(track), "queue_source": source,
                "played_at": (track.get("played_at") if isinstance(track, dict) else None) or time.time(),
                "_rotor_station": origin_station, "_rotor_batch_id": origin_batch}

    def _persist_playback(self) -> None:
        try:
            with self._playback_lock:
                state = {"previous": [self._stable_track(x, self._station) for x in self._history[-5:]],
                         "current": self._stable_track(self._current_track_obj, self._station) if self._current_track_obj else None,
                         "upcoming": [self._stable_track(x, self._station) for x in self._station_queue[:5]],
                         "station": self._station, "shuffle": self._shuffle_enabled, "repeat_mode": self._repeat_mode}
                ids = {_track_id(x) for x in [self._current_track_obj, *self._history[-5:], *self._station_queue[:5]] if x}
                paths = {self._cached_candidate_path(c) for key, (_, rows) in self._prepared_candidates.items()
                         if key in ids for c in rows}
                pin_cache(f"local:{id(self)}", paths - {""})
            self._session_store.save(state)
        except Exception:
            logger.warning("Could not save local playback history", exc_info=True)

    def on_network_changed(self, *_args: Any) -> None:
        """Invalidate pooled HTTP state without discarding account or playback."""
        # Notification threads must never block behind HTTP authentication or
        # downloads. The next client lookup lazily replaces its connection state.
        self._network_generation += 1
        try:
            from jarvis.runtime import http_pool173
            http_pool173.reset("yandex_music")      # NEO 174: the kept-alive connection belonged to the old network
        except Exception:
            pass
        self._api_last_attempt = 0.0
        self._wave_refill_retry_at = 0.0
        self._prepared_candidates = {k: v for k, v in list(self._prepared_candidates.items())
                                     if any(self._cached_candidate_path(c) for c in v[1])}

    @staticmethod
    def _youtube_queue_entry(item: dict[str, Any]) -> dict[str, Any]:
        video_id = str(item.get("video_id", "") or item.get("id", "") or "").strip()
        webpage_url = normalize_youtube_url(str(item.get("webpage_url", "") or item.get("url", "") or ""))
        if not webpage_url:
            raise RuntimeError("YouTube не вернул корректную ссылку элемента")
        title = str(item.get("title", "") or "YouTube").strip()
        artist = str(item.get("artist", "") or "YouTube").strip()
        return {
            "_source_type": "youtube",
            "media_source": "YouTube",
            "track_id": f"youtube:{video_id or webpage_url}",
            "id": f"youtube:{video_id or webpage_url}",
            "title": title,
            "artist": artist,
            "query": webpage_url,
            "_source_url": webpage_url,
            "_expected_duration_ms": int(item.get("duration_ms", 0) or 0),
        }

    @staticmethod
    def _is_youtube_entry(track: Any) -> bool:
        return isinstance(track, dict) and str(track.get("_source_type", "") or "").casefold() == "youtube"

    def local_youtube_transport_allowed(self, action: str) -> bool:
        """A disabled catalogue must not disable an already loaded YouTube song.

        This exception never opens an account/catalogue operation or crosses
        into a Yandex entry while advancing a mixed queue or playback history.
        """
        if action not in {"play", "play_pause", "pause", "stop", "next", "previous", "set_volume", "volume_up", "volume_down", "status", "background_status", "queue_status", "queue_clear",
                          "shuffle_on", "shuffle_off", "repeat_on", "repeat_track", "repeat_playlist", "repeat_off"}:
            return False
        session = local_playback_session(local_player())
        owner = session.owner()
        if owner is not None and owner is not self:
            return owner.local_youtube_transport_allowed(action)
        if session.source_epoch and owner is None:
            return False
        with self._playback_lock:
            if not self._is_youtube_entry(self._current_track_obj):
                return False
            if action == "next" and (not self._station_queue or not self._is_youtube_entry(self._station_queue[0])):
                return False
            if action == "previous" and (not self._history or not self._is_youtube_entry(self._history[-1])):
                return False
        # Eligibility belongs to the selected source, not the worker state.
        # A stopped/crashed worker may need rebuilding from this descriptor.
        return True

    def owns_loaded_source(self) -> bool:
        """True while this adapter's PC player holds a loaded track (any source)."""
        session = local_playback_session(local_player())
        owner = session.owner()
        if owner is not None and owner is not self:
            probe = getattr(owner, "owns_loaded_source", None)
            return bool(probe()) if callable(probe) else False
        if session.source_epoch and owner is None:
            return False
        with self._playback_lock:
            return self._current_track_obj is not None

    @_local_playback_request
    def _play_youtube_entry(
        self,
        entry: dict[str, Any],
        *,
        station: str | None = None,
        batch_id: str | None = None,
        queue: list[Any] | None = None,
        auto_advance: bool | None = None,
        remember_previous: bool = True,
        fast_transition: bool = False,
        expected_manual_serial: int | None = None,
        expected_shared_serial: int | None = None,
        queue_revision: int | None = None,
        queue_reconcile: Any = None,
    ) -> dict[str, Any]:
        with self._playback_lock:
            operation_serial = self._manual_transport_serial if expected_manual_serial is None else expected_manual_serial
        self._assert_shared_current(operation_serial, expected_shared_serial)
        """Play one YouTube audio item through the same hidden PC player."""
        clean = normalize_youtube_url(str(entry.get("_source_url", "") or entry.get("query", "") or ""))
        if not clean:
            raise RuntimeError("В очереди YouTube отсутствует корректная ссылка")
        cached_path = str(entry.get("_youtube_cached_path", "") or "").strip()
        prepared_meta = entry.get("_youtube_cached_meta") if isinstance(entry.get("_youtube_cached_meta"), dict) else {}
        if cached_path and os.path.isfile(cached_path):
            resolved = dict(prepared_meta or {})
            resolved["local_path"] = cached_path
        else:
            def check_download_cancelled():
                try:
                    self._assert_shared_current(operation_serial, expected_shared_serial)
                except PlaybackSuperseded as exc:
                    # Resolver cancellation must not poison its download-error cache.
                    raise MediaRequestSuperseded(str(exc)) from exc
            download = self._youtube.download_audio
            try:
                parameters = inspect.signature(download).parameters.values()
                cancellable = any(p.name == "check_cancelled" or p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters)
            except (ValueError, TypeError):
                cancellable = False
            try:
                resolved = download(clean, check_cancelled=check_download_cancelled) if cancellable else download(clean)
            except MediaRequestSuperseded as exc:
                raise PlaybackSuperseded(str(exc)) from exc
            cached_path = str(resolved.get("local_path", "") or "").strip()
        if not cached_path or not os.path.isfile(cached_path):
            raise RuntimeError("yt-dlp не подготовил локальный аудиофайл YouTube")
        title = str(resolved.get("title", "") or entry.get("title", "") or "YouTube").strip()
        artist = str(resolved.get("artist", "") or entry.get("artist", "") or "YouTube").strip()
        duration_ms = int(resolved.get("duration_ms", 0) or entry.get("_expected_duration_ms", 0) or 0)
        normalized_entry = {
            **entry,
            "title": title,
            "artist": artist,
            "query": clean,
            "_source_url": clean,
            "_expected_duration_ms": duration_ms,
        }
        self._transition_guard_until = time.monotonic() + 8.0
        player = local_player()
        self._guarded_player_volume(player, self._background_volume_percent(),
                    lambda: self._assert_shared_current(operation_serial, expected_shared_serial))
        self._assert_shared_current(operation_serial, expected_shared_serial)
        played = player.play(cached_path, title=title, artist=artist, fast_verify=fast_transition,
                             before_start=lambda: self._before_local_source_start(operation_serial, expected_shared_serial))
        if not bool(played.get("verified", False)):
            raise RuntimeError("Фоновый плеер не подтвердил воспроизведение аудио YouTube")

        with self._playback_lock:
            self._commit_local_source(operation_serial, expected_shared_serial)
            if remember_previous:
                self._after_playback_effect("history", self._remember_current)
            self._current_track_obj = normalized_entry
            self._current_track_meta = {
                "id": str(normalized_entry.get("id", "") or ""),
                "title": title,
                "artist": artist,
                "media_source": "YouTube",
                "webpage_url": clean,
                "duration_ms": duration_ms,
                "timeline_verified": bool(played.get("timeline_verified")),
                "player_url": str(played.get("url", "") or cached_path),
            }
            if station is not None:
                if str(station or "") != self._station:
                    self._wave_queue_generation += 1
                self._station = str(station or "")
            if batch_id is not None:
                self._station_batch_id = str(batch_id or "")
            if queue is not None:
                self._station_queue = self._reconciled_queue(list(queue), queue_revision, queue_reconcile)
            if auto_advance is not None:
                self._auto_advance = bool(auto_advance)
            now_mono = time.monotonic()
            self._current_started_monotonic = now_mono
            self._current_track_meta["played_at"] = time.time()
            self._playback_generation += 1
            playback_generation = self._playback_generation
            committed_epoch = int(expected_shared_serial or 0)
            self._manual_pause_active = False
            post_guard = 15.0 if fast_transition else 9.0
            self._transition_guard_until = max(self._transition_guard_until, now_mono + post_guard)
            self._manual_transport_guard_until = max(self._manual_transport_guard_until, now_mono + post_guard)

        if self._auto_advance:
            self._after_playback_effect("monitor_start", self._start_station_monitor)
            with self._playback_lock:
                next_prefetch_window = list(self._station_queue[:8])
            self._after_playback_effect("prefetch", self._start_prefetch_window, next_prefetch_window, limit=8)
            if str(self._station or "") == "user:onyourwave":
                self._after_playback_effect("queue_refill", self._ensure_wave_queue_depth, 8)

        self._after_playback_effect("session_save", self._persist_playback)
        media_event_id = self._after_playback_effect("now_playing", publish_now_playing,
            "youtube",
            title,
            artist,
            str(normalized_entry.get("id", "") or ""),
            duration_ms=int(played.get("duration_ms", 0) or duration_ms),
            position_ms=int(played.get("position_ms", 0) or 0),
            playing=str(played.get("state", "") or "").casefold() == "playing" and not bool(played.get("starting")),
            playback_generation=playback_generation,
            source_epoch=committed_epoch,
            source_scope=_session_event_scope(local_playback_session(local_player())),
        )
        return {
            **played,
            "app": "youtube_audio",
            "media_event_id": media_event_id,
            "track_id": str(normalized_entry.get("id", "") or ""),
            "title": title,
            "artist": artist,
            "webpage_url": clean,
            "media_source": "YouTube",
            "api_used": False,
            "method": "youtube_yt_dlp_local_cache+jarvis_background_qt",
            "non_invasive": True,
        }

    @_local_source_command
    def play_youtube_url(self, url: str, *, queue: bool = False, next_up: bool = False) -> dict[str, Any]:
        """Play/queue a YouTube video or playlist on this PC.

        This is an internal URL route, not a public Yandex catalog command.  It
        intentionally shares the existing JARVIS local transport/history/queue so
        next, previous, pause and stop continue to work across Yandex and YouTube.
        """
        if not self._background_enabled():
            raise RuntimeError("Фоновый медиаплеер NEO отключён")
        clean = normalize_youtube_url(str(url or ""))
        if not clean:
            raise ValueError("Нужна корректная ссылка YouTube")
        limit = max(1, min(500, int(self.config.get("integrations.yandex_music.youtube_playlist_limit", 200) or 200)))
        expanded = self._youtube.expand(clean, limit=limit)
        raw_items = [item for item in list(expanded.get("items") or []) if isinstance(item, dict)]
        entries = [self._youtube_queue_entry(item) for item in raw_items]
        if not entries:
            raise RuntimeError("YouTube-ссылка не вернула доступных элементов")

        queue_epoch = getattr(self._source_command_local, "queue_epoch", None)
        queue_serial = getattr(self._source_command_local, "queue_serial", None)
        session = local_playback_session(local_player())
        if queue_epoch is not None and (not session.queue_current(self, queue_epoch) or self._manual_transport_serial != queue_serial):
            raise PlaybackSuperseded("Очередь изменила владельца во время подготовки")
        with self._playback_lock:
            current = self._current_track_obj
            existing_queue = list(self._station_queue)
            old_station = str(self._station or "")

        # "Добавь в очередь" while something plays preserves finite/manual
        # pending items, but terminates generated recommendation tails (My Wave,
        # similar tracks) so the explicit request is not hidden behind 20 songs.
        if queue and current is not None:
            with self._playback_lock, session.lock:
                if queue_epoch is not None and (not session.queue_current(self, queue_epoch) or self._manual_transport_serial != queue_serial):
                    raise PlaybackSuperseded("Очередь изменила владельца во время подготовки")
                old_station = str(self._station or "")
                existing_queue = list(self._station_queue)
                if old_station == "user:onyourwave" or old_station.startswith("similar:"):
                    existing_queue = []
                staged = list(entries)
                combined = staged + existing_queue if next_up else existing_queue + staged
                if old_station == "user:onyourwave" or old_station.startswith("similar:") or not self._playlist_repeat_seed:
                    self._playlist_repeat_seed = ([current] if current is not None else []) + list(combined)
                else:
                    self._playlist_repeat_seed = list(self._playlist_repeat_seed) + staged
                self._station = "mixed:manual"
                self._station_batch_id = ""
                self._station_queue = combined
                self._auto_advance = True
                self._bump_queue_revision()
            self._start_station_monitor()
            self._start_prefetch_window(combined[:8], limit=8)
            self._persist_playback()
            self._publish_queue_update()
            return {
                "app": "youtube_audio", "verified": True, "queued": True,
                "queue_started_immediately": False, "added_count": len(entries),
                "queue_length": len(combined), "next_up": bool(next_up),
                "kind": str(expanded.get("kind", "video") or "video"),
                "playlist_title": str(expanded.get("title", "") or ""),
                "truncated": bool(expanded.get("truncated")), "webpage_url": clean,
                "non_invasive": True, "shared_local_queue": True,
            }

        # Empty queue + queue request starts immediately, mirroring Discord. A
        # direct "включи" also starts immediately and replaces the pending source.
        with self._playback_lock:
            # MEDIA185-U05: the loaded YouTube list is the repeat seed of this
            # finite queue, exactly like a Yandex playlist.
            self._playlist_repeat_seed = list(entries)
            repeat_active = str(self._repeat_mode or "off") != "off"
            shuffled = bool(self._shuffle_enabled and len(entries) > 1)
        ordered = self._cycle_from_seed(entries) if shuffled else list(entries)
        first = ordered[0]
        rest = list(ordered[1:])
        finite_station = "mixed:youtube"
        data = self._play_track_object(
            first, station=finite_station, batch_id="", queue=rest,
            # MEDIA185-U09: a single item still needs its end monitored when a
            # repeat mode is active.
            auto_advance=bool(rest) or repeat_active, remember_previous=True,
            fast_transition=current is not None,
        )
        return {
            **data,
            "queued": bool(rest), "queue_started_immediately": bool(queue),
            "added_count": len(entries), "queue_length": len(rest),
            "play_now": not bool(queue), "next_up": bool(next_up),
            "kind": str(expanded.get("kind", "video") or "video"),
            "playlist_title": str(expanded.get("title", "") or ""),
            "truncated": bool(expanded.get("truncated")), "shared_local_queue": True,
        }

    def _assert_transport_current(self, serial: int) -> None:
        with self._playback_lock:
            if int(self._manual_transport_serial) != int(serial):
                raise PlaybackSuperseded("Воспроизведение отменено более новой командой.")

    def _begin_local_transport(self, player, *, expected_epoch: int | None = None):
        session = local_playback_session(player)
        shared = session.begin_transport(self, expected_epoch=expected_epoch)
        if shared is None:
            raise PlaybackSuperseded("Источник фонового плеера уже изменён")
        return session, shared

    def _assert_shared_current(self, serial: int, shared: int) -> None:
        self._assert_transport_current(serial)
        if not local_playback_session(local_player()).current(shared):
            raise PlaybackSuperseded("Воспроизведение заменено более новым источником")

    def _before_local_source_start(self, serial: int, shared: int) -> None:
        self._assert_shared_current(serial, shared)
        if not local_playback_session(local_player()).mark_submitted(self, shared):
            raise PlaybackSuperseded("Запуск источника отменён более новой командой")

    def _commit_local_source(self, serial: int, shared: int) -> None:
        self._assert_shared_current(serial, shared)
        if not local_playback_session(local_player()).commit(self, shared):
            raise PlaybackSuperseded("Воспроизведение заменено более новым источником")
        self._local_source_epoch = shared

    @staticmethod
    def _guarded_player_volume(player, percent, guard):
        guarded = getattr(type(player), "guarded_volume", None)
        if callable(guarded):
            return guarded(player, percent, before_send=guard)
        guard()
        return player.set_volume(percent)

    @staticmethod
    def _guarded_player_transport(player, action, guard):
        # Native submission runs the check under the same lock as play IPC.
        guarded = getattr(type(player), "guarded_transport", None)
        if callable(guarded):
            return guarded(player, action, before_send=guard)
        guard()  # Inert/legacy transports without the native command lane.
        return getattr(player, action)()

    @staticmethod
    def _cancelled_playback_result() -> dict[str, Any]:
        return {"app": "yandex_music", "cancelled": True, "superseded": True,
                "verified": False, "non_invasive": True}

    def _after_playback_effect(self, label: str, callback, *args, **kwargs):
        """Run ancillary work without turning audible success into a retry."""
        outcomes = self.__dict__.setdefault("_effect_outcomes188", {})
        try:
            result = callback(*args, **kwargs)
        except Exception as exc:
            outcomes[label] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            logger.warning("Playback is active; post-start %s failed", label, exc_info=True)
            return None
        outcomes[label] = {"ok": True, "error": ""}
        return result

    @_local_playback_request
    def _play_track_object(
        self,
        track: Any,
        *,
        station: str | None = None,
        batch_id: str | None = None,
        queue: list[Any] | None = None,
        auto_advance: bool | None = None,
        remember_previous: bool = True,
        fast_transition: bool = False,
        stream_first: bool = False,
        expected_manual_serial: int | None = None,
        expected_shared_serial: int | None = None,
        queue_revision: int | None = None,
        queue_reconcile: Any = None,
    ) -> dict[str, Any]:
        with self._playback_lock:
            operation_serial = self._manual_transport_serial if expected_manual_serial is None else expected_manual_serial
            queue_station_at_prepare = self._station
            queue_generation_at_prepare = self._wave_queue_generation
            queue_baseline_ids = {_track_id(item) for item in self._station_queue}
        self._assert_shared_current(operation_serial, expected_shared_serial)
        selected_station = str(station if station is not None else self._station)
        batch_id = self._batch_for_track(track, selected_station, str(batch_id or ""))
        if not self._enabled() and not self._is_youtube_entry(track):
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        track = self._materialize_track(track)
        if self._is_youtube_entry(track):
            return self._play_youtube_entry(
                track, station=station, batch_id=batch_id, queue=queue,
                auto_advance=auto_advance, remember_previous=remember_previous,
                fast_transition=fast_transition, expected_manual_serial=operation_serial, expected_shared_serial=expected_shared_serial,
                queue_revision=queue_revision, queue_reconcile=queue_reconcile,
            )
        if track is None or not hasattr(track, "get_download_info"):
            raise RuntimeError("Яндекс Музыка не вернула полные данные трека для воспроизведения")
        meta = {"id": _track_id(track), "title": _track_title(track), "artist": _artists_text(track), "object": track}
        candidates = self._stream_candidates_for_track(track, meta)
        if not candidates:
            raise RuntimeError(f"Яндекс Музыка не вернула доступный аудиопоток для «{meta['title'] or meta['id']}»")

        if stream_first or fast_transition:
            responsive = [c for c in candidates if 0 < int(c.get("bitrate_kbps", 0) or 0) <= 192]
            if responsive:
                candidates = responsive + [c for c in candidates if c not in responsive]

        # HOTFIX45: for a manual next/previous, a fully cached candidate is more
        # valuable than waiting for the highest-bitrate direct stream to buffer.
        # Prefetch may deliberately cache a smaller candidate first so a spoken
        # transport command can start from disk almost immediately. Preserve the
        # original quality order when no local candidate is ready.
        if fast_transition:
            ready_candidates = [c for c in candidates if self._cached_candidate_path(c)]
            if ready_candidates:
                ready_ids = {id(c) for c in ready_candidates}
                candidates = ready_candidates + [c for c in candidates if id(c) not in ready_ids]

        # The local player briefly reports stopped/end-of-media while replacing
        # one source with another.  Ignore that transition in the station monitor
        # so one spoken "next" cannot trigger a second automatic skip.
        self._transition_guard_until = time.monotonic() + 8.0

        last_error: Exception | None = None
        played: dict[str, Any] | None = None
        chosen: dict[str, Any] | None = None
        for candidate in candidates:
            try:
                player = local_player()
                self._guarded_player_volume(player, self._background_volume_percent(),
                    lambda: self._assert_shared_current(operation_serial, expected_shared_serial))
                try:
                    ready_cache = self._cached_candidate_path(candidate)
                    track_id_for_cache = str(candidate.get("id", "") or "")
                    with self._prefetch_lock:
                        prefetch_in_progress = bool(track_id_for_cache and track_id_for_cache in self._prefetching_ids)
                    if ready_cache:
                        source = ready_cache
                        source_method = "cached_local_file_ready"
                    elif prefetch_in_progress or fast_transition or stream_first:
                        # HOTFIX41: manual next/previous must acknowledge immediately.
                        # Never start a blocking full-song download on the transport
                        # hot path; direct-stream now while any background prefetch
                        # continues (or let the following item be prefetched).
                        source = str(candidate["url"])
                        source_method = "direct_stream_fast_transition" if fast_transition else "direct_stream_start" if stream_first else "direct_stream_prefetch_in_progress"
                    else:
                        source = self._cache_candidate(candidate)
                        source_method = "cached_local_file"
                except Exception as cache_exc:
                    # Keep a direct-stream fallback so temporary CDN/download
                    # problems do not turn a playable track into a hard failure.
                    logger.warning("Yandex full-track cache failed; trying direct stream: %s", cache_exc)
                    source = str(candidate["url"])
                    source_method = "direct_stream_fallback"
                # Source replacement is the precise point where Qt can briefly
                # report stopped/end-of-media for the old item. Refresh the guard
                # here as cache/download preparation may have taken several seconds.
                self._transition_guard_until = time.monotonic() + 3.2
                self._assert_shared_current(operation_serial, expected_shared_serial)
                played = player.play(
                    source,
                    title=str(candidate.get("title", "") or ""),
                    artist=str(candidate.get("artist", "") or ""),
                    fast_verify=fast_transition,
                    before_start=lambda: self._before_local_source_start(operation_serial, expected_shared_serial),
                )
                self._assert_shared_current(operation_serial, expected_shared_serial)
                played["source_method"] = source_method

                # When an account/CDN unexpectedly returns only a preview or a
                # truncated object, Qt can still report "playing".  Compare the
                # media duration with Yandex track metadata before accepting it.
                expected_ms = int(candidate.get("expected_duration_ms", 0) or 0)
                actual_ms = int(played.get("duration_ms", 0) or 0)
                if expected_ms > 60_000 and actual_ms <= 0:
                    deadline = time.monotonic() + (0.10 if fast_transition else 1.2)
                    while time.monotonic() < deadline and actual_ms <= 0:
                        time.sleep(0.025 if fast_transition else 0.07)
                        try:
                            probe = player.status()
                        except Exception:
                            logger.warning("Playback is active; duration probe unavailable", exc_info=True)
                            break
                        actual_ms = int(probe.get("duration_ms", 0) or 0)
                        if actual_ms > 0:
                            played.update(probe)
                            break
                if expected_ms > 60_000 and actual_ms > 0 and actual_ms < int(expected_ms * 0.72):
                    try:
                        self._guarded_player_transport(player, "stop",
                            lambda: self._assert_shared_current(operation_serial, expected_shared_serial))
                    except PlaybackSuperseded:
                        raise
                    except Exception:
                        pass
                    last_error = RuntimeError(
                        f"Яндекс Музыка вернула укороченный поток: {actual_ms/1000:.0f} с вместо ожидаемых {expected_ms/1000:.0f} с"
                    )
                    logger.warning("Rejected short Yandex stream for track %s: %s", meta.get("id"), last_error)
                    continue

                if bool(played.get("verified", False)):
                    chosen = candidate
                    break
                last_error = RuntimeError("плеер сообщил Playing, но позиция трека не начала двигаться")
            except PlaybackSuperseded:
                raise
            except Exception as exc:
                self._assert_shared_current(operation_serial, expected_shared_serial)
                last_error = exc
                logger.warning(
                    "Background Yandex stream failed codec=%s bitrate=%s: %s",
                    candidate.get("codec"), candidate.get("bitrate_kbps"), exc,
                )
                if self._note_network_failure(exc):
                    raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.") from exc
        if chosen is None or played is None:
            if last_error is not None and self._note_network_failure(last_error):
                raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.") from last_error
            raise RuntimeError(f"Фоновый плеер не смог открыть ни один поток Яндекс Музыки: {last_error or 'unknown error'}")

        with self._playback_lock:
            self._commit_local_source(operation_serial, expected_shared_serial)
            if remember_previous:
                self._after_playback_effect("history", self._remember_current)
            if selected_station == "user:onyourwave" and _track_id(self._current_track_obj) != _track_id(track):
                self._wave_feedback_ready.clear()
            self._current_track_obj = track
            self._current_track_meta = {k: v for k, v in chosen.items() if k not in {"object", "url"}}
            if station is not None:
                if str(station or "") != self._station:
                    self._wave_queue_generation += 1
                self._station = str(station or "")
            if batch_id is not None:
                self._station_batch_id = str(batch_id or "")
            if queue is not None:
                prepared_queue = list(queue)
                if (selected_station == queue_station_at_prepare
                        and self._wave_queue_generation == queue_generation_at_prepare):
                    # A refill may append songs while the selected source is
                    # preparing. Preserve only those new entries, never items
                    # intentionally consumed/removed from the original snapshot.
                    known_ids = {_track_id(item) for item in [track, *prepared_queue]}
                    known_fp = {_track_fingerprint(item) for item in [track, *prepared_queue]}
                    for item in self._station_queue:
                        tid, fp = _track_id(item), _track_fingerprint(item)
                        if tid and tid not in queue_baseline_ids and tid not in known_ids and (not fp or fp not in known_fp):
                            prepared_queue.append(item)
                            known_ids.add(tid)
                            if fp:
                                known_fp.add(fp)
                self._station_queue = self._reconciled_queue(prepared_queue, queue_revision, queue_reconcile)
            if auto_advance is not None:
                self._auto_advance = bool(auto_advance)
            # The new track is now the authoritative current item. Keep a longer
            # post-commit guard and track its start time. This prevents stale Qt
            # EndOfMedia/near-end state from the previous source from causing a
            # second skip after a manual «дальше».
            now_mono = time.monotonic()
            self._current_started_monotonic = now_mono
            self._current_track_meta["played_at"] = time.time()
            self._current_track_meta["last_verified_position_ms"] = int(played.get("position_ms", 0) or 0)
            self._current_track_meta["timeline_verified"] = bool(played.get("timeline_verified"))
            self._current_track_meta["source_method"] = str(played.get("source_method", ""))
            self._current_track_meta["player_url"] = str(played.get("url", "") or source or "")
            self._playback_generation += 1
            playback_generation = self._playback_generation
            committed_epoch = int(expected_shared_serial or 0)
            self._manual_pause_active = False
            self._network_notice_active = False
            post_guard = 15.0 if fast_transition else 9.0
            self._transition_guard_until = max(self._transition_guard_until, now_mono + post_guard)
            self._manual_transport_guard_until = max(self._manual_transport_guard_until, now_mono + post_guard)

        if self._station == "user:onyourwave":
            self._after_playback_effect("wave_history", self._remember_wave_track, track)
        if self._auto_advance:
            self._after_playback_effect("monitor_start", self._start_station_monitor)
            with self._playback_lock:
                # HOTFIX41: the shuffle queue is already a randomized bag. Its
                # first pending item is therefore just as deterministic as the
                # sequential next item and can be prefetched safely. HOTFIX40.2
                # accidentally disabled this path whenever shuffle=True, which
                # explains the ~7 s manual next latency visible in field logs.
                next_prefetch_window = ([track] if stream_first else []) + list(self._station_queue[:5])
            self._after_playback_effect("prefetch", self._start_prefetch_window, next_prefetch_window, limit=6)
            # Keep My Wave materially ahead of playback after every successful
            # transition.  The refill itself is asynchronous and generation-
            # cancellable, so this does not add command latency.

        logger.info("Yandex playback committed: track=%s state=%s media=%s position_ms=%s duration_ms=%s source=%s timeline=%s",
            meta.get("id"), played.get("state"), played.get("media_status"),
            played.get("position_ms"), played.get("duration_ms"), played.get("source_method"), played.get("timeline_verified"))
        # Event-driven UI notification. This is intentionally in-process and does
        # not poll the Yandex desktop app, GSMTC or browser state. It fires only
        # after the hidden player has accepted the new track.
        self._after_playback_effect("session_save", self._persist_playback)
        media_event_id = self._after_playback_effect("now_playing", publish_now_playing,
            "yandex_music",
            str(chosen.get("title", "") or meta.get("title", "") or ""),
            str(chosen.get("artist", "") or meta.get("artist", "") or ""),
            str(chosen.get("id", "") or meta.get("id", "") or ""),
            duration_ms=int(played.get("duration_ms", 0) or chosen.get("duration_ms", 0) or meta.get("duration_ms", 0) or 0),
            position_ms=int(played.get("position_ms", 0) or 0),
            playing=str(played.get("state", "") or "").casefold() == "playing" and not bool(played.get("starting")),
            playback_generation=playback_generation,
            source_epoch=committed_epoch,
            source_scope=_session_event_scope(local_playback_session(local_player())),
        )
        return {
            **played,
            "app": "yandex_music",
            "media_event_id": media_event_id,
            "track_id": str(chosen.get("id", "") or ""),
            "title": str(chosen.get("title", "") or ""),
            "artist": str(chosen.get("artist", "") or ""),
            "codec": chosen.get("codec"),
            "bitrate_kbps": chosen.get("bitrate_kbps"),
            "api_used": True,
            "method": "yandex_api+jarvis_background_qt",
            "non_invasive": True,
        }

    def _station_tracks(self, station: str, *, queue_track_id: str = "") -> tuple[list[Any], str]:
        client = self._api_client()
        if client is None:
            raise RuntimeError(self._api_unavailable_message())

        def fetch() -> Any:
            with self._api_lock:
                request = getattr(client, 'request', None)
                if client.__class__.__module__.startswith('yandex_music') and callable(getattr(request, 'get', None)):
                    from yandex_music import StationTracksResult
                    params = {'settings2': 'True'}
                    if queue_track_id: params['queue'] = str(queue_track_id)
                    raw = request.get(f'{client.base_url}/rotor/station/{station}/tracks', params=params, timeout=5.0)
                    return StationTracksResult.de_json(raw, client)
                return client.rotor_station_tracks(station, settings2=True, queue=(queue_track_id or None))

        try:
            result = self._retry(fetch, f"station {station}", attempts=1)
        except Exception as exc:
            self._api_last_error_kind = self._yandex_error_kind(exc)
            raise
        self._api_last_error_kind = ""
        if result is None:
            raise RuntimeError(f"Яндекс Музыка не вернула последовательность станции {station}")
        sequence = list((result.get('sequence') if isinstance(result, dict) else getattr(result, 'sequence', None)) or [])
        tracks: list[Any] = []
        for item in sequence:
            track = item.get('track') if isinstance(item, dict) else getattr(item, 'track', None)
            if track is not None:
                tracks.append(track)
        batch_id = str((result.get('batch_id') or result.get('batchId', '')) if isinstance(result, dict) else getattr(result, 'batch_id', '') or '')
        if not tracks:
            raise RuntimeError(f"В станции {station} не найдено доступных треков")
        with self._track_batch_lock:
            for track in tracks:
                self._track_batches[id(track)] = (track, station, batch_id)
            while len(self._track_batches) > 512:
                self._track_batches.pop(next(iter(self._track_batches)))
        return tracks, batch_id

    def _station_feedback(self, type_: str, track_id: str = "", *, seconds: float = 0.0) -> None:
        with self._playback_lock:
            station, batch_id = self._station, self._station_batch_id
            if station == "user:onyourwave" and type_ in {"skip", "trackFinished"}:
                self._wave_last_completed_id = str(track_id or "")
                key = (self._playback_generation, station, str(track_id or ""))
                if self._completed_feedback_key == key:
                    return  # Retrying a queue read must not replay listening mutations.
                self._completed_feedback_key = key
        # Personal-library/YouTube queues are not Rotor stations.
        if not station or station.startswith(("similar:", "playlist:", "likes:", "mixed:", "youtube:")):
            return
        if not self._api_enabled():
            return
        self._enqueue_station_feedback(station, batch_id, type_, track_id, seconds=seconds)
        if station == "user:onyourwave" and type_ == "trackStarted":
            self._wave_feedback_ready.set()
            self._ensure_wave_queue_depth(8)

    def submit_wave_feedback(self, type_: str, track_id: str, *, batch_id: str = "", seconds: float = 0.0) -> None:
        """Actual Discord playback events, without modifying desktop transport state."""
        if type_ not in {"trackStarted", "trackFinished", "skip"} or not track_id or not self._api_enabled():
            return
        self._enqueue_station_feedback("user:onyourwave", batch_id, type_, track_id, seconds=seconds)

    def _enqueue_station_feedback(self, station: str, batch_id: str, type_: str, track_id: str, *, seconds: float = 0.0) -> None:
        """Serialize account-bound events; never retry an ambiguous mutation."""
        import hashlib
        account = hashlib.sha256(str(getattr(self, "_client_token", "")).encode()).hexdigest()
        completed = threading.Event()
        with self._playback_lock:
            self._feedback_pending = [e for e in self._feedback_pending if not e.is_set()][-31:]
            self._feedback_pending.append(completed)
        submitted = self._feedback_lane.submit({"station": station, "batch_id": batch_id, "account": account,
            "type": type_, "track_id": str(track_id), "seconds": max(0.0, float(seconds)),
            "timestamp": time.time(), "_completed": completed})
        if not submitted:
            completed.set()

    def _wait_wave_feedback(self, timeout: float = 3.0) -> None:
        """Background continuation observes submitted real feedback in order."""
        with self._playback_lock:
            pending = list(self._feedback_pending)
        deadline = time.monotonic() + timeout
        for completed in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not completed.wait(remaining):
                break  # Ambiguous feedback is never replayed; next refill can retry read.

    def _send_station_feedback(self, event: dict[str, Any]) -> None:
        try:
            self._send_station_feedback_now(event)
        finally:
            completed = event.get("_completed")
            if completed is not None:
                completed.set()

    def _send_station_feedback_now(self, event: dict[str, Any]) -> None:
        """Executed only on the feedback lane, never on the media control path."""
        if not self._api_enabled():
            return
        token = yandex_music_token(self.config)
        if not token:
            return
        import hashlib
        if event.get("account") != hashlib.sha256(token.encode()).hexdigest():
            return  # Queued feedback belongs to the previous account.
        if self._feedback_client is None or token != self._feedback_token or self._feedback_generation != self._network_generation:
            from yandex_music import Client
            from yandex_music.utils.request import Request
            # Rotor feedback needs the token, not another account/status init().
            from jarvis.integrations.yandex_pool174 import make_request
            self._feedback_client = Client(token, request=make_request(self.config, timeout=2.5))
            self._feedback_token = token
            self._feedback_generation = self._network_generation
        kwargs = {"from_": "neo-jarvis-background", "timestamp": event["timestamp"], "timeout": 2.5}
        if event["type"] != "radioStarted":
            kwargs["batch_id"] = event["batch_id"] or None
        if event["track_id"]:
            kwargs["track_id"] = event["track_id"]
        if event["type"] in {"skip", "trackFinished"}:
            kwargs["total_played_seconds"] = event["seconds"]
        # No retry after a timeout: the server may already have accepted it.
        self._feedback_client.rotor_station_feedback(event["station"], event["type"], **kwargs)

    def _similar_tracks(self, track_id: str) -> list[Any]:
        client = self._api_client()
        if client is None:
            raise RuntimeError("API Яндекс Музыки недоступен для подбора следующего трека")
        def fetch() -> Any:
            with self._api_lock:
                return client.tracks_similar(track_id)
        result = self._retry(fetch, f"similar tracks {track_id}")
        values = list(getattr(result, "similar_tracks", None) or []) if result is not None else []
        return [item for item in values if item is not None]

    def _observed_auto_next(self, manual_serial: int, shared_epoch: int) -> dict[str, Any]:
        """Carry a monitor observation into atomic admission, never reuse it as admission."""
        local = self._source_command_local
        previous = getattr(local, "auto_observation", None)
        local.auto_observation = (manual_serial, shared_epoch)
        try:
            return self._next_background(auto=True)
        finally:
            local.auto_observation = previous

    @staticmethod
    def _coalesced_next_result() -> dict[str, Any]:
        return {"app": "yandex_music", "action": "next", "already_advancing": True,
                "coalesced": True, "starting": True, "verified": False,
                "requested": True, "non_invasive": True}

    def _next_background(self, *, auto: bool = False, expected_manual_serial: int | None = None, expected_shared_serial: int | None = None,
                         advance_claimed: bool = False) -> dict[str, Any]:
        observation = getattr(self._source_command_local, "auto_observation", None) if auto else None
        observed_shared_epoch = None
        if observation is not None:
            expected_manual_serial, observed_shared_epoch = observation
        if not advance_claimed and not self._advance_lock.acquire(blocking=False):
            return {**self._coalesced_next_result(), "coalesced": not auto}
        session = local_playback_session(local_player())
        shared_serial = expected_shared_serial
        try:
            with self._playback_lock:
                if auto and self._manual_pause_active:
                    return {"app": "yandex_music", "action": "next", "cancelled": True,
                            "verified": False, "manual_pause": True, "non_invasive": True}
                operation_serial = self._manual_transport_serial if expected_manual_serial is None else expected_manual_serial
                self._assert_transport_current(operation_serial)
                if not session.owned_by(self):
                    raise PlaybackSuperseded("Источник фонового плеера уже изменён")
                if shared_serial is None:
                    shared_serial = session.begin_recovery(self, expected_epoch=observed_shared_epoch) if auto else session.begin_transport(self)
                    if shared_serial is None:
                        raise PlaybackSuperseded("Источник изменён или готовится более новая команда")
                self._assert_shared_current(operation_serial, shared_serial)
                self._advance_operation = ("next", operation_serial, shared_serial)
                current = self._current_track_obj
                current_id = _track_id(current) if current is not None else str(self._current_track_meta.get("id", "") or "")
                station = self._station or ("mixed:youtube" if self._is_youtube_entry(current) else f"track:{current_id}" if current_id else "")
                queue = list(self._station_queue)
                queue_revision = int(getattr(self, "_queue_revision", 0))
                batch_id = self._station_batch_id
                auto_advance = self._auto_advance
                shuffle_enabled = bool(self._shuffle_enabled)
                repeat_mode = str(self._repeat_mode or "off").casefold()
                playlist_repeat_seed = list(self._playlist_repeat_seed)
            if not current_id:
                raise RuntimeError("Нет текущего фонового трека для команды «следующий»")
            if not station:
                raise RuntimeError("Не удалось построить станцию для следующего трека")

            try:
                playback_status = local_player().status_if_running() or {}
            except Exception:
                playback_status = {}
            position_ms = int(playback_status.get("position_ms", 0) or 0)
            duration_ms = int(playback_status.get("duration_ms", 0) or 0)
            played_seconds = max(0.0, position_ms / 1000.0)
            if auto and duration_ms > 0:
                # Rotor feedback expects the actually-listened duration. At a
                # natural end use the known duration rather than the old HOTFIX
                # behaviour of reporting 0 seconds for every finished track.
                played_seconds = max(played_seconds, duration_ms / 1000.0)
            if not self._is_youtube_entry(current):
                self._after_playback_effect("finished_feedback", self._station_feedback, "trackFinished" if auto else "skip", current_id, seconds=played_seconds)

            if auto and repeat_mode == "track" and current is not None:
                data = self._play_track_object(
                    current, expected_manual_serial=operation_serial, expected_shared_serial=shared_serial, station=station, batch_id=batch_id, queue=queue,
                    auto_advance=auto_advance, remember_previous=False, fast_transition=True,
                    queue_revision=queue_revision, queue_reconcile=lambda live: live,
                )
                if not self._is_youtube_entry(current):
                    self._after_playback_effect("track_feedback", self._station_feedback, "trackStarted", current_id)
                return {**data, "action": "next", "station": station, "auto_advance": auto_advance, "repeat_mode": "track", "repeated": True}

            wave_recent_ids, wave_recent_fp = self._wave_recent_sets()
            with self._playback_lock:
                recent_ids = {_track_id(item) for item in self._history[-32:] if _track_id(item)}
                recent_fp = {_track_fingerprint(item) for item in self._history[-32:] if _track_fingerprint(item)}
                if station == "user:onyourwave":
                    recent_ids.update(wave_recent_ids)
                    recent_fp.update(wave_recent_fp)
            recent_ids.add(current_id)
            if current is not None:
                current_fp = _track_fingerprint(current)
                if current_fp:
                    recent_fp.add(current_fp)

            # Capture the floor even for a similar-track source: this operation
            # may later fall back to My Wave. Apply it only to the Wave source.
            floor_ids, floor_fp = self._wave_playback_floor()
            def usable(values: list[Any], *, strict_recent: bool = True) -> list[Any]:
                result: list[Any] = []
                seen: set[str] = set()
                seen_fp: set[str] = set()
                for track in values or []:
                    tid = _track_id(track)
                    fp = _track_fingerprint(track)
                    if not tid or tid == current_id or tid in seen or (station == "user:onyourwave" and tid in floor_ids):
                        continue
                    if fp and (fp in seen_fp or (station == "user:onyourwave" and fp in floor_fp)):
                        continue
                    if strict_recent and (tid in recent_ids or (fp and fp in recent_fp)):
                        continue
                    seen.add(tid)
                    if fp:
                        seen_fp.add(fp)
                    result.append(track)
                return result

            finite = self._finite_station(station)
            playlist_repeat = bool(finite and repeat_mode == "playlist")
            original_pending = list(queue)
            if finite:
                # MEDIA185-U06: an explicit finite queue keeps its declared
                # occurrences and order (A,B,A,C stays A,B,A,C); radio
                # anti-repeat filtering does not apply to it.
                queue = [item for item in queue if _track_id(item)]
            else:
                queue = usable(queue, strict_recent=not playlist_repeat)
            if not queue and station == "user:onyourwave":
                queue = usable(original_pending, strict_recent=False)
            fetched_raw: list[Any] = []
            if not queue:
                if playlist_repeat and not playlist_repeat_seed and current is not None and len(original_pending) == 0:
                    # A single loaded item still loops in repeat-playlist mode.
                    playlist_repeat_seed = [current]
                if playlist_repeat and playlist_repeat_seed:
                    # A one-track playlist is a valid playlist loop; otherwise
                    # start a complete new cycle from the immutable seed (the
                    # finished track is part of the cycle, at its own place).
                    if len(playlist_repeat_seed) == 1 and current is not None:
                        data = self._play_track_object(
                    current, expected_manual_serial=operation_serial, expected_shared_serial=shared_serial, station=station, batch_id=batch_id, queue=[],
                            auto_advance=auto_advance, remember_previous=False, fast_transition=True,
                        )
                        if not self._is_youtube_entry(current):
                            self._after_playback_effect("track_feedback", self._station_feedback, "trackStarted", current_id)
                        return {**data, "action": "next", "station": station, "auto_advance": auto_advance, "repeat_mode": "playlist", "playlist_wrapped": True}
                    queue = self._cycle_from_seed(playlist_repeat_seed, avoid_first=current_id)
                if not queue and (station.startswith("playlist:") or station.startswith("mixed:") or station.startswith("youtube:")):
                    # Named playlists and HOTFIX75 mixed/YouTube queues are finite.
                    # At their end stop cleanly instead of feeding their marker to
                    # the Yandex Rotor station API.
                    self._assert_shared_current(operation_serial, shared_serial)
                    stopped = self._guarded_player_transport(local_player(), "stop",
                        lambda: self._assert_shared_current(operation_serial, shared_serial))
                    with self._playback_lock:
                        self._assert_shared_current(operation_serial, shared_serial)
                        session.mark_stopped(shared_serial)
                        self._clear_background_sequence(keep_current=True)
                    return {
                        "app": "yandex_music", "action": "next", "playlist_finished": True,
                        "state": str(stopped.get("state", "stopped")), "verified": bool(stopped.get("verified")), "non_invasive": True,
                    }
                if station == "likes:library":
                    try:
                        refreshed = list(self._liked_tracks() or [])
                    except Exception as exc:
                        logger.warning("Yandex liked-tracks refresh failed: %s", exc)
                        refreshed = []
                    queue = usable(refreshed) or usable(refreshed, strict_recent=False)
                    if self._shuffle_enabled and len(queue) > 1:
                        random.SystemRandom().shuffle(queue)
                elif station.startswith("similar:"):
                    # Similar-track endpoints can transiently return only recently
                    # played items. Retry once and finally relax history filtering
                    # rather than failing a perfectly valid "next" command.
                    for _ in range(2):
                        try:
                            fetched_raw = list(self._similar_tracks(current_id) or [])
                        except Exception as exc:
                            logger.warning("Yandex similar-track refresh failed: %s", exc)
                            fetched_raw = []
                        queue = usable(fetched_raw)
                        if queue:
                            break
                    if not queue:
                        queue = usable(fetched_raw, strict_recent=False)
                    if not queue:
                        # Last-resort continuity for a directly requested track:
                        # if the similar-tracks endpoint has no usable continuation,
                        # continue with the user's personalized Rotor station instead
                        # of dropping into silence. This is still recommendations,
                        # never the static "My Tracks" library.
                        try:
                            wave_tracks, wave_batch = self._station_tracks("user:onyourwave", queue_track_id=current_id)
                            queue = usable(list(wave_tracks or [])) or usable(list(wave_tracks or []), strict_recent=False)
                            if queue:
                                station = "user:onyourwave"
                                batch_id = wave_batch
                                auto_advance = True
                                logger.info("Yandex next fell back from similar tracks to My Wave continuity")
                        except Exception as exc:
                            logger.warning("Yandex My Wave continuity fallback failed: %s", exc)
                else:
                    # Rotor/My Wave sometimes returns a batch dominated by the
                    # previous queue. For My Wave reuse the persistent anti-repeat
                    # resolver; for other stations keep the generic refresh path.
                    last_raw: list[Any] = []
                    if station == "user:onyourwave":
                        for queue_hint in (current_id,):
                            try:
                                wave = self.wave_tracks(
                                    queue_track_id=queue_hint, avoid_ids=floor_ids | {current_id}, fresh_start=False, fast_start=not auto
                                )
                                if wave.get("network_unavailable"):
                                    raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.")
                                last_raw = list(wave.get("tracks") or [])
                                new_batch = str(wave.get("batch_id", "") or "")
                                if new_batch:
                                    batch_id = new_batch
                            except Exception as exc:
                                logger.warning("Yandex My Wave refresh failed: %s", exc)
                                if self._note_network_failure(exc):
                                    raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.") from exc
                                continue
                            queue = usable(last_raw) or usable(last_raw, strict_recent=False)
                            if queue:
                                break
                    else:
                        for queue_hint in (current_id,):
                            try:
                                fetched, new_batch = self._station_tracks(station, queue_track_id=queue_hint)
                                last_raw = list(fetched or [])
                                if new_batch:
                                    batch_id = new_batch
                            except Exception as exc:
                                logger.warning("Yandex station refresh failed for %s: %s", station, exc)
                                continue
                            queue = usable(last_raw)
                            if queue:
                                break
                    if not queue:
                        if station != "user:onyourwave":
                            queue = usable(last_raw, strict_recent=False)
                        else:
                            # HOTFIX127 continuity tier.  The diversity resolver
                            # remains first choice, but after a very long listening
                            # session its recent-history window can cover every item
                            # in several Rotor batches.  Query Rotor directly and
                            # relax only the history filter before ever dropping
                            # playback into silence.
                            try:
                                continuity_raw, continuity_batch = self._station_tracks(
                                    "user:onyourwave", queue_track_id=current_id
                                )
                                queue = usable(list(continuity_raw or []), strict_recent=False)
                                if queue and continuity_batch:
                                    batch_id = continuity_batch
                                if queue:
                                    logger.info(
                                        "Yandex My Wave continuity relaxed recent history after fresh pool exhaustion; candidates=%d",
                                        len(queue),
                                    )
                            except Exception as exc:
                                logger.warning("Yandex direct My Wave continuity refresh failed: %s", exc)
            if not queue:
                raise RuntimeError("Яндекс Музыка не предложила следующий доступный трек после повторных запросов")
            # Do not let one bad CDN/media candidate drop My Wave into silence.
            # Try several distinct queued tracks before giving up. The current
            # track is not committed/replaced in JARVIS state until a candidate
            # actually verifies, so we can rebuild it as a continuity fallback.
            original_queue = list(queue)
            # HOTFIX63: a *manual* next is a transaction for exactly one song.
            # Older code tried up to three different queue items when Qt was slow
            # to report timeline movement; the user heard 2-3 skips and then the
            # old song was restored.  Automatic recovery may still seek another
            # candidate, but one button/voice command can select only one track.
            attempts = min(4 if auto else 1, len(queue))
            errors: list[str] = []
            for attempt_index in range(attempts):
                next_track = self._pop_sequence_track(queue)
                logger.info(
                    "Yandex %s next selected%s: %s — %s (%s) shuffle=%s remaining=%d",
                    "automatic" if auto else "manual",
                    f" attempt={attempt_index + 1}/{attempts}" if attempts > 1 else "",
                    _artists_text(next_track), _track_title(next_track), _track_id(next_track),
                    shuffle_enabled, len(queue),
                )
                try:
                    data = self._play_track_object(
                    next_track, expected_manual_serial=operation_serial, expected_shared_serial=shared_serial, station=station, batch_id=batch_id, queue=queue,
                        auto_advance=auto_advance, remember_previous=True, fast_transition=not auto,
                        queue_revision=queue_revision,
                        queue_reconcile=lambda live, consumed=next_track: self._without_occurrence(live, consumed),
                    )
                except PlaybackSuperseded:
                    raise
                except Exception as exc:
                    self._assert_shared_current(operation_serial, shared_serial)
                    errors.append(f"{_track_id(next_track)}: {exc}")
                    logger.warning(
                        "Yandex next candidate failed%s: %s",
                        "; automatic recovery may try another" if auto else "; manual command will not skip another song",
                        exc,
                    )
                    if self._note_network_failure(exc):
                        # Preserve the candidate and player; a network outage is
                        # not evidence of a broken song or Qt process.
                        raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.") from exc
                    # A candidate error does not prove the worker is broken.
                    # Preparation can fail before replacing the audible source.
                    # Native unresponsive-worker recovery belongs to the monitor.
                    continue
                if not self._is_youtube_entry(next_track):
                    self._after_playback_effect("track_feedback", self._station_feedback, "trackStarted", _track_id(next_track))
                if station == "user:onyourwave":
                    self._after_playback_effect("wave_history", self._remember_wave_track, next_track)
                preview = self._after_playback_effect("queue_preview", self._queue_preview_state, queue_limit=8, history_limit=5) or {}
                return {
                    **data, **preview, "action": "next",
                    "station": station, "auto_advance": auto_advance,
                }

            # All candidate replacements failed. Re-open the previously
            # authoritative track so the player remains controllable and the user
            # can say «продолжи» instead of being left with a dead source.
            if auto and current is not None and station != "user:onyourwave":
                try:
                    logger.warning("Yandex automatic next exhausted candidates; restoring previous track to avoid silence")
                    # REC-AUTO-RESTORE-OMITTED-REVISION-01: the restored A keeps the
                    # captured snapshot's revision; a newer confirmed edit (C,B)
                    # wins and restoring A consumes no pending occurrence.
                    self._play_track_object(
                        current, expected_manual_serial=operation_serial, expected_shared_serial=shared_serial, station=station, batch_id=batch_id, queue=original_queue,
                        auto_advance=auto_advance, remember_previous=False, fast_transition=True,
                        queue_revision=queue_revision, queue_reconcile=lambda live: live,
                    )
                except PlaybackSuperseded:
                    raise
                except Exception as restore_exc:
                    errors.append(f"restore-current: {restore_exc}")
                    logger.warning("Yandex current-track continuity restore failed: %s", restore_exc)
            raise RuntimeError("Не удалось запустить следующий трек: " + "; ".join(errors[-4:]))
        except PlaybackSuperseded:
            return self._cancelled_playback_result()
        finally:
            with self._playback_lock:
                active = getattr(self, "_advance_operation", None)
                if active and active[2] == shared_serial:
                    self._advance_operation = None
            if shared_serial is not None:
                session.finish(shared_serial)
            self._advance_lock.release()

    def _previous_background(self, *, expected_manual_serial: int | None = None, expected_shared_serial: int | None = None,
                             advance_claimed: bool = False) -> dict[str, Any]:
        if not advance_claimed and not self._advance_lock.acquire(blocking=False):
            raise RuntimeError("Переключение трека уже выполняется")
        session = local_playback_session(local_player())
        shared_serial = expected_shared_serial
        try:
            with self._playback_lock:
                operation_serial = self._manual_transport_serial if expected_manual_serial is None else expected_manual_serial
                self._assert_transport_current(operation_serial)
                if not session.owned_by(self):
                    raise PlaybackSuperseded("Источник фонового плеера уже изменён")
                if shared_serial is None:
                    shared_serial = session.begin_transport(self)
                    if shared_serial is None:
                        raise PlaybackSuperseded("Источник фонового плеера уже изменён")
                self._assert_shared_current(operation_serial, shared_serial)
                self._advance_operation = ("previous", operation_serial, shared_serial)
                if not self._history:
                    raise RuntimeError("В истории фонового плеера ещё нет предыдущего трека")
                track = self._history[-1]
                station, batch_id = self._station, self._station_batch_id
                current = self._current_track_obj
                queue = ([current] if current is not None else []) + list(self._station_queue)
                queue_revision = int(getattr(self, "_queue_revision", 0))
                auto_advance = self._auto_advance
            # Commit history only once the transport has actually confirmed play.
            data = self._play_track_object(
                track, expected_manual_serial=operation_serial, expected_shared_serial=shared_serial, station=station, batch_id=batch_id, queue=queue,
                auto_advance=auto_advance, remember_previous=False, fast_transition=True,
                queue_revision=queue_revision,
                queue_reconcile=lambda live, old=current: ([old] if old is not None else []) + list(live),
            )
            with self._playback_lock:
                self._assert_shared_current(operation_serial, shared_serial)
                self._history.pop()
            if station == "user:onyourwave":
                self._after_playback_effect("previous_track_feedback", self._station_feedback, "trackStarted", _track_id(track))
            self._after_playback_effect("session_save", self._persist_playback)
            preview = self._after_playback_effect("queue_preview", self._queue_preview_state, queue_limit=5, history_limit=5) or {}
            return {**data, **preview, "action": "previous", "station": station}
        except PlaybackSuperseded:
            return self._cancelled_playback_result()
        finally:
            with self._playback_lock:
                active = getattr(self, "_advance_operation", None)
                if active and active[2] == shared_serial:
                    self._advance_operation = None
            if shared_serial is not None:
                session.finish(shared_serial)
            self._advance_lock.release()

    def _start_station_monitor(self) -> None:
        # 186: a monitor belongs to one stop-event generation. A retiring
        # monitor (its event already set) is never mistaken for the live one;
        # the new sequence gets its own event and thread.
        with self._playback_lock:
            if not self._auto_advance:
                return
            thread = self._monitor_thread
            if thread is not None and thread.is_alive() and not self._monitor_stop.is_set():
                return
            stop = threading.Event()
            self._monitor_stop = stop
            self._monitor_thread = threading.Thread(
                target=self._station_monitor, args=(stop,), name="jarvis-yandex-station-monitor", daemon=True
            )
            thread = self._monitor_thread
        thread.start()

    def _discard_incomplete_track_cache(self, track_id: str) -> None:
        """A premature local EOF invalidates only this owned transient cache."""
        _, candidates = self._prepared_candidates.get(str(track_id), (0.0, []))
        root = self._cache_root().resolve()
        for candidate in candidates:
            cached = self._cached_candidate_path(candidate)
            if not cached:
                continue
            path = Path(cached)
            try:
                if path.resolve().parent == root:
                    path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove incomplete owned music cache", exc_info=True)
        self._prepared_candidates.pop(str(track_id), None)

    def _station_monitor(self, stop: threading.Event | None = None) -> None:
        stop = stop if stop is not None else self._monitor_stop
        was_playing = False
        recovery_track_id = ""
        observed_track_id = ""
        observed_started = 0.0
        observed_playing_since = 0.0
        pending_auto_retry_at = 0.0
        pending_auto_retry_count = 0
        observed_manual_serial = -1
        last_source_epoch = -1
        pending_network_retry_at = 0.0
        network_retry_count = 0
        network_failed_since = 0.0
        recovery_limit = max(10.0, float(self.config.get("integrations.yandex_music.network_recovery_seconds", 60) or 60))
        network_position = 0
        last_progress_position = -1
        last_progress_at = 0.0
        last_status_signature = None
        best_position = 0
        next_refill_probe_at = 0.0
        while not stop.wait(0.45):
            session = local_playback_session(local_player())
            if not session.owned_by(self):
                return
            if session.preparing_owner() is not None:
                continue
            with self._playback_lock:
                if not self._auto_advance or not self._station:
                    return
                current_obj = self._current_track_obj
                current_id = _track_id(current_obj) if current_obj is not None else str(self._current_track_meta.get("id", "") or "")
                station = self._station
                batch_id = self._station_batch_id
                queue = list(self._station_queue)
                manual_pause = bool(self._manual_pause_active)
                manual_guard = float(self._manual_transport_guard_until or 0.0)
                manual_serial = int(self._manual_transport_serial or 0)
                current_started = float(self._current_started_monotonic or 0.0)
                current_meta = dict(self._current_track_meta)
                observed_shared_epoch = session.epoch
                source_epoch = session.source_epoch
            if manual_serial != observed_manual_serial or source_epoch != last_source_epoch:
                # A manual transport invalidates any automatic retry that was
                # scheduled for the previous media source. This prevents one tap
                # on «Далее» from being followed by a stale monitor-driven skip.
                observed_manual_serial = manual_serial
                last_source_epoch = source_epoch
                pending_auto_retry_at = 0.0
                pending_auto_retry_count = 0
                recovery_track_id = ""
                was_playing = False
                observed_playing_since = 0.0
                pending_network_retry_at = 0.0
                network_retry_count = 0
                last_progress_position = -1
                last_progress_at = 0.0
            # A manually requested next/play changes current_track_id before/while
            # Qt tears down the previous media source. Reset edge-detection for the
            # new track and wait until that track is actually observed as playing.
            if current_id and (current_id != observed_track_id or current_started != observed_started):
                track_changed = bool(observed_track_id) and current_id != observed_track_id
                observed_track_id = current_id
                observed_started = current_started
                best_position = int(current_meta.get("last_verified_position_ms", 0) or 0)
                was_playing = bool(current_meta.get("timeline_verified"))
                recovery_track_id = ""
                # MUSIC185-AUTO-01: a verified timeline *is* observed playback.
                # Without its start time played_for stayed 0 and the natural end
                # of «Мои треки» never advanced automatically.
                observed_playing_since = current_started if was_playing and current_started > 0 else 0.0
                if track_changed and pending_auto_retry_at:
                    # The current track really changed: the old operation's
                    # retry must not advance the new one a second time.
                    pending_auto_retry_at = 0.0
                    pending_auto_retry_count = 0
                pending_network_retry_at = 0.0
                network_retry_count = 0
                last_progress_position = -1
                last_progress_at = 0.0
            now_mono = time.monotonic()
            if manual_pause:
                # A user pause is authoritative. HOTFIX56 checked a previously
                # scheduled auto-next retry *before* this flag; that retry could
                # therefore start another track immediately after Telegram had
                # successfully paused the player. Cancel retry state while paused.
                pending_auto_retry_at = 0.0
                pending_auto_retry_count = 0
                recovery_track_id = ""
                was_playing = False
                observed_playing_since = 0.0
                pending_network_retry_at = 0.0
                continue
            if station == "user:onyourwave" and len(queue) < 5 and now_mono >= next_refill_probe_at:
                next_refill_probe_at = now_mono + 5.0
                self._after_playback_effect("ongoing_wave_refill", self._ensure_wave_queue_depth, 8)
            if pending_network_retry_at:
                if now_mono < pending_network_retry_at:
                    continue
                if not self._advance_lock.acquire(blocking=False):
                    continue
                recovery_shared = None
                try:
                    # Retry the same source after a transport interruption. A
                    # network failure never grants permission to skip songs.
                    with self._playback_lock:
                        self._assert_transport_current(manual_serial)
                        recovery_shared = session.begin_recovery(self, expected_epoch=observed_shared_epoch)
                        if recovery_shared is None:
                            raise PlaybackSuperseded("Источник изменён или готовится более новая команда")
                    recreate = getattr(local_player(), "recreate_if_unresponsive", None)
                    if callable(recreate) and recreate(before_restart=lambda: self._assert_shared_current(manual_serial, recovery_shared)):
                        logger.warning("Yandex owned player worker was unresponsive; recreating same-track playback")
                    from jarvis.runtime.media_events import command_result_owns_media_notice
                    with command_result_owns_media_notice():
                        self._play_track_object(current_obj, station=station, batch_id=batch_id,
                            queue=queue, auto_advance=True, remember_previous=False,
                            fast_transition=True, expected_manual_serial=manual_serial, expected_shared_serial=recovery_shared)
                    seek = getattr(local_player(), "seek", None)
                    if network_position > 0 and callable(seek):
                        seek_result = seek(network_position, before_send=lambda: self._assert_shared_current(manual_serial, recovery_shared))
                        if isinstance(seek_result, dict) and seek_result.get("seek_applied") is False:
                            logger.warning("Yandex source recovered but seek is unsupported; preserving resumed source")
                    pending_network_retry_at = 0.0
                    network_retry_count = 0
                    network_failed_since = 0.0
                    recovery_track_id = ""
                    was_playing = False
                    observed_playing_since = 0.0
                except PlaybackSuperseded:
                    pending_network_retry_at = 0.0
                except Exception as exc:
                    network_retry_count += 1
                    if not network_failed_since:
                        network_failed_since = time.monotonic()
                    self._note_network_failure(exc)
                    if time.monotonic() - network_failed_since > recovery_limit:
                        # NEO 177: recovery is for a short drop. Much later the user has moved on;
                        # music must not start on its own when the network comes back.
                        logger.warning("Yandex network recovery gave up after %.0fs; playback stopped", recovery_limit)
                        try:
                            self._guarded_player_transport(local_player(), "stop",
                                lambda: self._assert_shared_current(manual_serial, recovery_shared))
                            with self._playback_lock:
                                self._assert_shared_current(manual_serial, recovery_shared)
                                session.mark_stopped(recovery_shared)
                                self._clear_background_sequence(keep_current=False)
                        except PlaybackSuperseded:
                            pass
                        return
                    pending_network_retry_at = time.monotonic() + min(20.0, 2.0 * 2 ** min(3, network_retry_count))
                    logger.warning("Yandex same-track network recovery deferred: %s", type(exc).__name__)
                finally:
                    if recovery_shared is not None:
                        session.finish(recovery_shared)
                    self._advance_lock.release()
                continue
            if pending_auto_retry_at and now_mono >= pending_auto_retry_at:
                try:
                    self._observed_auto_next(manual_serial, observed_shared_epoch)
                    pending_auto_retry_at = 0.0
                    pending_auto_retry_count = 0
                    network_failed_since = 0.0
                    recovery_track_id = ""
                    was_playing = False
                    observed_playing_since = 0.0
                    continue
                except Exception as exc:
                    pending_auto_retry_count += 1
                    if not network_failed_since:
                        network_failed_since = now_mono
                    if now_mono - network_failed_since > recovery_limit:
                        logger.warning("Yandex automatic next gave up after %.0fs; the station is stopped", recovery_limit)
                        self._clear_background_sequence(keep_current=False)
                        return
                    delay = min(12.0, 1.2 * (2 ** min(3, pending_auto_retry_count - 1)))
                    pending_auto_retry_at = now_mono + delay
                    logger.warning(
                        "Yandex automatic next retry %s deferred %.1fs: %s",
                        pending_auto_retry_count, delay, exc,
                    )
                    continue
            if pending_auto_retry_at:
                # REC-MONITOR-PERSISTENT-EOF-RETRY-DEADLINE-01: this operation
                # already has a scheduled retry. A persistent EOF waits for its
                # monotonic deadline instead of starting another auto-next and
                # resetting the failure series.
                continue
            in_guard = now_mono < max(self._transition_guard_until, manual_guard)
            bound_url = str(current_meta.get("player_url", "") or "")
            if in_guard and not bound_url:
                continue
            try:
                status = local_player().status_if_running()
            except Exception as exc:
                logger.warning("Yandex player status unavailable: %s", type(exc).__name__)
                status = None
            # MEDIA185-U12: the worker resets url/position on every new source,
            # so an EndOfMedia carrying the committed source URL belongs to the
            # current item, not to the replaced one.
            source_bound = bool(bound_url and status and str(status.get("url", "") or "") == bound_url)
            if in_guard:
                status_media = str((status or {}).get("media_status", "") or "").casefold()
                terminal = "endofmedia" in status_media or "end_of_media" in status_media
                if not (source_bound and terminal and not (status or {}).get("error")):
                    continue
            with self._playback_lock:
                observation_current = (self._manual_transport_serial == manual_serial
                                       and session.current(observed_shared_epoch))
            if not observation_current:
                continue
            if not status:
                # A crashed/disappeared worker cannot produce EndOfMedia. Keep
                # station ownership and retry this source, subject to pause/stop.
                if current_obj is not None and now_mono - current_started >= 12.0:
                    pending_network_retry_at = now_mono + 2.0
                    network_position = best_position
                continue
            state = str(status.get("state", "") or "").casefold()
            media = str(status.get("media_status", "") or "").casefold()
            error = str(status.get("error", "") or "").strip()
            position = int(status.get("position_ms", 0) or 0)
            duration = int(status.get("duration_ms", 0) or 0)
            expected_duration = int(current_meta.get("expected_duration_ms", 0) or current_meta.get("duration_ms", 0) or getattr(current_obj, "duration_ms", 0) or 0)
            best_position = max(best_position, position)
            signature = (current_id, state, media, str(status.get("error_kind", "")))
            if signature != last_status_signature:
                logger.info("Yandex player observation: track=%s state=%s media=%s error_kind=%s position_ms=%s duration_ms=%s expected_ms=%s serial=%s",
                    *signature, position, duration, expected_duration, manual_serial)
                last_status_signature = signature
            if last_progress_position < 0 or position != last_progress_position:
                last_progress_position = position
                last_progress_at = now_mono
            # Some Qt backends remain Playing+StalledMedia indefinitely after
            # losing a connection, without an error signal. Require both stalled
            # buffering state and a genuinely stationary timeline before recovery.
            stalled = (state != "paused" and any(x in media for x in ("stalled", "buffering"))
                and last_progress_at > 0 and now_mono - last_progress_at >= 15.0)
            if stalled:
                self._note_network_failure(ConnectionError("buffering stalled without timeline progress"))
                network_position = max(0, position)
                pending_network_retry_at = time.monotonic() + 2.0
                pending_auto_retry_at = 0.0
                was_playing = False
                last_progress_at = now_mono
                continue
            if (status.get("error_kind") == "network" or
                    error and self._yandex_error_kind(RuntimeError(error)) == "network"):
                self._note_network_failure(ConnectionError(error or "Qt network interruption"))
                network_position = max(0, position)
                pending_network_retry_at = time.monotonic() + 2.0
                pending_auto_retry_at = 0.0
                was_playing = False
                continue
            # A remote EOF can erase Qt's duration/position before the monitor
            # observes it. The API duration and last observed position distinguish
            # a truncated stream from a real ending. Never skip on such an EOF.
            ended_early = (state == "stopped" and ("endofmedia" in media or "end_of_media" in media)
                and expected_duration > 15000 and best_position < expected_duration - 3000
                and now_mono - current_started >= 8.0)
            interrupted_after_progress = (state == "stopped" and bool(error) and best_position >= 40
                and expected_duration > 15000 and best_position < expected_duration - 3000)
            if ended_early or interrupted_after_progress:
                logger.warning("Yandex premature media end: track=%s observed_ms=%s expected_ms=%s; restoring same track", current_id, best_position, expected_duration)
                if "cached_local_file" in str(current_meta.get("source_method", "")):
                    self._discard_incomplete_track_cache(current_id)
                else:
                    self._note_network_failure(ConnectionError("premature stream end"))
                network_position = best_position
                pending_network_retry_at = now_mono + 2.0
                pending_auto_retry_at = 0.0
                was_playing = False
                continue
            if state == "playing":
                if not was_playing or observed_playing_since <= 0:
                    observed_playing_since = now_mono
                was_playing = True
                if current_id and recovery_track_id != current_id:
                    recovery_track_id = ""
                continue
            if state == "paused":
                # Preserve the user's pause even if the explicit pause flag was
                # cleared by a backend restart; a paused player is not an end edge.
                was_playing = False
                observed_playing_since = 0.0
                continue
            ended = "endofmedia" in media or "end_of_media" in media
            end_duration = max(duration, expected_duration)
            end_position = best_position if ended else position
            near_end = bool(end_duration > 0 and end_position >= max(0, end_duration - 1800))
            played_for = now_mono - observed_playing_since if observed_playing_since > 0 else 0.0
            current_age = now_mono - current_started if current_started > 0 else 0.0
            # A fresh source must have been genuinely playing for several seconds
            # before an end event is trusted. This blocks the characteristic
            # manual-next -> new track starts -> stale old EndOfMedia -> second skip.
            trustworthy_end = was_playing and played_for >= 10.0 and current_age >= 10.0 and (
                near_end or (ended and ((end_duration > 0 and end_position >= max(0, end_duration - 2500)) or (end_duration <= 0 and end_position >= 30_000)))
            )
            if not trustworthy_end and source_bound and ended and not error and end_duration > 0:
                # A short item (2–12 s) ends before the 10 s observation window.
                # Its own source reached EndOfMedia at its known duration after
                # at least most of that duration really elapsed.
                min_age = max(0.5, min(10.0, end_duration / 1000.0 * 0.7))
                trustworthy_end = (end_position >= max(0, end_duration - 1500)
                                   and current_age >= min_age)
            if trustworthy_end:
                logger.info(
                    "Yandex station monitor accepted natural end: track=%s position=%.1fs duration=%.1fs played_for=%.1fs",
                    current_id, position / 1000.0, duration / 1000.0 if duration else 0.0, played_for,
                )
                was_playing = False
                observed_playing_since = 0.0
                recovery_track_id = ""
                try:
                    self._observed_auto_next(manual_serial, observed_shared_epoch)
                    pending_auto_retry_at = 0.0
                    pending_auto_retry_count = 0
                except Exception as exc:
                    pending_auto_retry_count = 1
                    pending_auto_retry_at = time.monotonic() + 1.2
                    logger.warning("Yandex automatic next failed; source remains active and will retry: %s", exc)
                continue

            # A stopped/error state significantly before the known duration is a
            # premature playback failure, not the end of the song. Retry the same
            # fully cached track once before moving on.
            premature = (was_playing or (bool(error) and current_age >= 12.0)) and (bool(error) or (duration > 5000 and position < duration - 3000))
            if premature and current_obj is not None and current_id and recovery_track_id != current_id:
                try:
                    logger.warning(
                        "Yandex background playback stopped early at %.1fs/%.1fs; retrying current track once",
                        position / 1000.0, duration / 1000.0 if duration else 0.0,
                    )
                    if not self._advance_lock.acquire(blocking=False):
                        continue
                    try:
                        with self._playback_lock:
                            superseded = (self._manual_pause_active or
                                          self._manual_transport_serial != manual_serial or
                                          _track_id(self._current_track_obj) != current_id)
                        if superseded:
                            continue
                        recovery_shared = session.begin_recovery(self, expected_epoch=observed_shared_epoch)
                        if recovery_shared is None:
                            raise PlaybackSuperseded("Источник изменён или готовится более новая команда")
                        recovery_track_id = current_id
                        was_playing = False
                        try:
                            self._play_track_object(
                                current_obj, station=station, batch_id=batch_id, queue=queue,
                                auto_advance=True, remember_previous=False, expected_manual_serial=manual_serial,
                                expected_shared_serial=recovery_shared,
                            )
                        finally:
                            session.finish(recovery_shared)
                    finally:
                        self._advance_lock.release()
                    continue
                except Exception as exc:
                    logger.warning("Yandex premature-track recovery failed: %s", exc)
                    if self._note_network_failure(exc):
                        network_position = max(0, position)
                        pending_network_retry_at = time.monotonic() + 2.0
                        pending_auto_retry_at = 0.0
                        was_playing = False
                        continue
            if premature and recovery_track_id == current_id:
                was_playing = False
                try:
                    self._observed_auto_next(manual_serial, observed_shared_epoch)
                    pending_auto_retry_at = 0.0
                    pending_auto_retry_count = 0
                except Exception as exc:
                    pending_auto_retry_count = 1
                    pending_auto_retry_at = time.monotonic() + 1.2
                    logger.warning("Yandex recovery fallback next failed; will retry without ending station: %s", exc)

    @_local_source_command
    @_abandon_on_failure
    def play_track(self, query: str, artist: str = "") -> dict[str, Any]:
        if not self._enabled():
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        raw_query = str(query or "").strip()
        query = _clean_spoken_track_query(raw_query) or raw_query
        artist = str(artist or "").strip()
        if not query:
            raise ValueError("Не указано название трека")

        if self._background_enabled():
            resolved = None
            attempted_queries: list[str] = []
            for candidate in (_spoken_query_candidates(raw_query) or [query]):
                attempted_queries.append(candidate)
                resolved = self.resolve_track(candidate, artist)
                if resolved and resolved.get("object") is not None:
                    query = candidate
                    break
            if resolved and resolved.get("object") is not None:
                # A selected track becomes the seed of a continuous similar-track
                # queue. When it ends JARVIS requests documented similar tracks and
                # continues automatically, while remaining fully headless.
                track = resolved["object"]
                try:
                    seed_queue = self._similar_tracks(_track_id(track))[:20]
                except Exception as exc:
                    # Playback itself is more important than recommendations; if
                    # similar-track discovery is temporarily unavailable, next()
                    # will retry at the end of the song.
                    logger.warning("Yandex similar-track prequeue failed: %s", exc)
                    seed_queue = []
                data = self._play_track_object(
                    track, station=f"similar:{_track_id(track)}", batch_id="", queue=seed_queue,
                    auto_advance=True, remember_previous=True,
                )
                return {**data, "requested_query": query, "requested_artist": artist}
            if self._strict_non_invasive():
                status = self.api_status()
                if not status.get("token"):
                    raise RuntimeError(
                        "Для фонового поиска нужен OAuth-токен Яндекс Музыки. "
                        "Откройте Настройки → Медиа → Яндекс Музыка и нажмите «Подключить Яндекс Музыку»."
                    )
                raise RuntimeError(
                    f"Не удалось найти или получить аудиопоток для «{query}». "
                    "NEO повторил API-запросы и не запускал UI fallback."
                )

        raise RuntimeError(
            f"Не удалось запустить «{query}» через фоновый API/player Яндекс Музыки. "
            "Desktop/UI fallback отключён архитектурой v4.0.11."
        )

    @staticmethod
    def _control_descriptor(control: Any) -> str:
        try:
            return _norm(" ".join([
                str(control.window_text() or ""),
                str(getattr(control.element_info, "name", "") or ""),
                str(getattr(control.element_info, "help_text", "") or ""),
                str(getattr(control.element_info, "automation_id", "") or ""),
            ]))
        except Exception:
            return ""

    @classmethod
    def _find_controls(cls, window: Any, include: tuple[str, ...], *, exclude: tuple[str, ...] = ()) -> list[Any]:
        matches: list[tuple[int, Any]] = []
        try:
            controls = list(window.descendants())
        except Exception:
            return []
        for control in controls[:1500]:
            desc = cls._control_descriptor(control)
            if not desc or any(x in desc for x in exclude):
                continue
            score = sum(5 for x in include if x in desc)
            if score:
                try:
                    ctype = str(getattr(control.element_info, "control_type", "") or "")
                    if ctype in {"Button", "Hyperlink", "ListItem", "Text"}:
                        score += 2
                except Exception:
                    pass
                matches.append((score, control))
        matches.sort(key=lambda item: item[0], reverse=True)
        return [control for _, control in matches]

    @classmethod
    def _wave_ui_state(cls, window: Any) -> str:
        """Return playing/paused for the exact My Wave control when exposed."""
        for control in cls._find_controls(window, ("моя волна", "моей волны", "мою волну", "my wave", "on your wave")):
            desc = cls._control_descriptor(control)
            if not (re.search(r"\bмо\w*\s+волн\w*\b", desc) or "my wave" in desc or "on your wave" in desc):
                continue
            if "пауза" in desc or "pause" in desc:
                return "playing"
            if "воспроизвед" in desc or re.search(r"\bplay\b", desc):
                return "paused"
        return "unknown"

    @classmethod
    def _bottom_player_state(cls, window: Any) -> str:
        try:
            wr = window.rectangle() if callable(getattr(window, "rectangle", None)) else window.element_info.rectangle
            cutoff = int(wr.top) + int((int(wr.bottom) - int(wr.top)) * 0.70)
        except Exception:
            cutoff = -10**9
        states: list[tuple[int, str]] = []
        for control in cls._find_controls(window, ("пауза", "pause", "воспроизвед", "play"), exclude=("трейлер", "trailer")):
            try:
                ctype = str(getattr(control.element_info, "control_type", "") or "")
                rect = control.rectangle() if callable(getattr(control, "rectangle", None)) else control.element_info.rectangle
                if ctype != "Button" or int(rect.top) < cutoff:
                    continue
            except Exception:
                continue
            desc = cls._control_descriptor(control)
            if "пауза" in desc or "pause" in desc:
                states.append((20, "playing"))
            elif "воспроизвед" in desc or re.search(r"\bplay\b", desc):
                states.append((10, "paused"))
        states.sort(reverse=True)
        return states[0][1] if states else "unknown"

    def _native_transport_hotkey(self, action: str) -> dict[str, Any]:
        launch_yandex_music(wait_seconds=20.0)
        window = focus_yandex_music_window()
        if window is None:
            raise RuntimeError("Не вижу окно приложения Яндекс Музыка")
        desired = "playing" if action == "play" else "paused" if action in {"pause", "stop"} else ""
        before = self._bottom_player_state(window)
        if desired and before == desired:
            return {
                "app": "yandex_music", "action": action, "status": before,
                "method": "native_app_hotkey_noop", "verified": True,
                "already_in_requested_state": True,
            }
        key = {"play": "k", "pause": "k", "stop": "k", "next": "n", "previous": "p"}[action]
        import pyautogui
        try:
            window.set_focus()
        except Exception:
            pass
        pyautogui.press(key)
        if desired:
            deadline = time.monotonic() + 2.5
            last = "unknown"
            while time.monotonic() < deadline:
                last = self._bottom_player_state(window)
                if last == desired:
                    return {
                        "app": "yandex_music", "action": action, "status": last,
                        "method": f"native_app_hotkey_{key}", "verified": True,
                    }
                time.sleep(0.12)
            # A focused application hotkey was sent exactly once. Do not toggle
            # again if Electron's accessibility tree is late/unavailable.
            return {
                "app": "yandex_music", "action": action, "status": last,
                "method": f"native_app_hotkey_{key}", "verified": False,
                "control_success": True,
            }
        return {
            "app": "yandex_music", "action": action,
            "method": f"native_app_hotkey_{key}", "verified": True,
            "control_success": True,
        }

    @staticmethod
    def _invoke(control: Any) -> bool:
        for method in ("invoke", "click_input", "click"):
            fn = getattr(control, method, None)
            if not callable(fn):
                continue
            try:
                fn()
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _interaction_chain(control: Any, max_parents: int = 4) -> list[Any]:
        """Return a UIA text node plus clickable ancestors, without duplicates.

        Yandex Music's Electron UI often puts the accessible name on a Text child
        while the actual click handler lives on its parent card/button.
        """
        result: list[Any] = []
        seen: set[int] = set()
        current = control
        for _ in range(max(1, int(max_parents)) + 1):
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            result.append(current)
            try:
                parent = getattr(current, "parent", None)
                current = parent() if callable(parent) else None
            except Exception:
                break
        return result

    def _click_control_verified(self, control: Any, before: dict[str, Any] | None, *, label: str) -> dict[str, Any] | None:
        for target in self._interaction_chain(control):
            for method in ("invoke", "click_input", "click"):
                fn = getattr(target, method, None)
                if not callable(fn):
                    continue
                try:
                    fn()
                except Exception:
                    continue
                confirmed = self._wait_wave_playback(before, timeout=2.0)
                if confirmed is not None:
                    return {**confirmed, "method": f"native_uia_{label}_{method}", "verified": True}

            # Last local fallback: click the center of this *matched Yandex control*
            # instead of using a hard-coded screen coordinate. This survives UI
            # rearrangements and still cannot escape to a browser/window elsewhere.
            try:
                rect = target.rectangle() if callable(getattr(target, "rectangle", None)) else target.element_info.rectangle
                left, right, top, bottom = int(rect.left), int(rect.right), int(rect.top), int(rect.bottom)
                if right - left >= 8 and bottom - top >= 8:
                    import pyautogui
                    pyautogui.click((left + right) // 2, (top + bottom) // 2)
                    confirmed = self._wait_wave_playback(before, timeout=2.0)
                    if confirmed is not None:
                        return {**confirmed, "method": f"native_uia_{label}_center_click", "verified": True}
            except Exception:
                pass
        return None

    def _click_control(self, control: Any) -> bool:
        for target in self._interaction_chain(control):
            if self._invoke(target):
                return True
            try:
                rect = target.rectangle() if callable(getattr(target, "rectangle", None)) else target.element_info.rectangle
                left, right, top, bottom = int(rect.left), int(rect.right), int(rect.top), int(rect.bottom)
                if right - left >= 8 and bottom - top >= 8:
                    import pyautogui
                    pyautogui.click((left + right) // 2, (top + bottom) // 2)
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _playback_signature(data: dict[str, Any] | None) -> tuple[str, str, str]:
        data = dict(data or {})
        return (
            _norm(str(data.get("title", "") or "")),
            _norm(str(data.get("artist", "") or "")),
            _norm(str(data.get("status", "") or "")),
        )

    def _wait_wave_playback(self, before: dict[str, Any] | None, *, timeout: float = 5.0) -> dict[str, Any] | None:
        before_sig = self._playback_signature(before)
        deadline = time.monotonic() + max(0.8, float(timeout))
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            # UI Automation is authoritative for the exact My Wave button: the
            # accessible label changes from Play to Pause when it is running.
            try:
                window = focus_yandex_music_window()
                if window is not None and self._wave_ui_state(window) == "playing":
                    return {
                        "app": "yandex_music", "status": "playing",
                        "wave": True, "verification": "native_wave_button",
                    }
            except Exception:
                pass
            try:
                last = music_app_status("yandex_music")
                after_sig = self._playback_signature(last)
                playing = "playing" in after_sig[2]
                changed = after_sig[:2] != before_sig[:2] or "playing" not in before_sig[2]
                if playing and changed:
                    return last
            except Exception:
                pass
            time.sleep(0.18)
        return None

    def _dismiss_bad_route_dialog(self, window: Any | None) -> bool:
        if window is None:
            return False
        bad = self._find_controls(window, ("такой страницы нет", "страница не найдена", "page not found"))
        if not bad:
            return False
        buttons = self._find_controls(window, ("ок", "okay", "закрыть", "close"))
        for control in buttons[:6]:
            if self._invoke(control):
                return True
        try:
            import pyautogui
            pyautogui.press("esc")
            return True
        except Exception:
            return False

    def _try_wave_ui(self, window: Any, before: dict[str, Any] | None) -> dict[str, Any] | None:
        for attempt in range(2):
            # Prefer the exact accessibility action documented by Yandex:
            # “Воспроизведение «Моей волны»”. If it already says Pause, Wave is
            # already running and no click is needed.
            state = self._wave_ui_state(window)
            if state == "playing":
                return {
                    "app": "yandex_music", "status": "playing", "wave": True,
                    "method": "native_uia_my_wave_noop", "verified": True,
                }
            wave_controls = self._find_controls(
                window, ("моя волна", "моей волны", "мою волну", "my wave", "on your wave"), exclude=("настро", "settings")
            )
            exact: list[Any] = []
            generic: list[Any] = []
            for control in wave_controls:
                desc = self._control_descriptor(control)
                if not (re.search(r"\bмо\w*\s+волн\w*\b", desc) or "my wave" in desc or "on your wave" in desc):
                    continue
                if "воспроизвед" in desc or re.search(r"\bplay\b", desc):
                    exact.append(control)
                else:
                    generic.append(control)
            for control in (exact + generic)[:12]:
                confirmed = self._click_control_verified(control, before, label="my_wave")
                if confirmed is not None:
                    return confirmed
            if attempt == 0:
                radio = self._find_controls(window, ("радио", "radio"), exclude=("подкаст", "настро"))
                for control in radio[:6]:
                    if self._click_control(control):
                        time.sleep(0.55)
                        break

        # Final native-only geometry fallback. Chromium/Electron sometimes
        # renders visible controls without publishing useful UIA descendants. In
        # that case the old implementation failed even though the Radio/My Wave
        # screen was visibly open. Coordinates are relative to the *verified
        # Yandex Music window*, never to the desktop or a browser.
        try:
            rect = window.rectangle() if callable(getattr(window, "rectangle", None)) else window.element_info.rectangle
            width = max(1, int(rect.right) - int(rect.left))
            height = max(1, int(rect.bottom) - int(rect.top))
            import pyautogui

            wave_visible = bool(self._find_controls(window, ("моя волна", "моей волны", "мою волну", "my wave")))
            if not wave_visible:
                # Current desktop layout: Radio is the second item in the left
                # navigation rail. Open it first so the following click has a
                # deterministic target even when UIA is almost empty.
                pyautogui.click(int(rect.left + width * 0.080), int(rect.top + height * 0.185))
                time.sleep(0.80)

            # Current desktop Radio layout: My Wave is the first large card.
            pyautogui.click(int(rect.left + width * 0.325), int(rect.top + height * 0.405))
            confirmed = self._wait_wave_playback(before, timeout=3.2)
            if confirmed is not None:
                return {**confirmed, "method": "native_window_my_wave_card", "verified": True}

            # A deterministic click inside the verified native app is still much
            # more useful than turning a successfully dispatched command into a
            # fatal tool error solely because this Electron build exposes neither
            # a GSMTC session nor accessible button state. Mark that distinction
            # explicitly so callers do not pretend we observed playback.
            return {
                "app": "yandex_music", "status": "requested", "wave": True,
                "method": "native_window_my_wave_card", "verified": False,
                "command_sent": True,
            }
        except Exception:
            return None

    def _active_wave155(self):
        with self._playback_lock:
            active = self._station == "user:onyourwave" and self._auto_advance
            meta = dict(self._current_track_meta)
        if not active:
            return None
        status = local_player().status_if_running() or {}
        if str(status.get("state", "")).casefold() != "playing" or status.get("error"):
            return None
        return {**meta, **status, "app": "yandex_music", "wave": True,
                "station": "user:onyourwave", "station_kind": "my_wave",
                "verified": True, "unchanged": True, "non_invasive": True,
                "method": "yandex_active_wave"}

    @_local_source_command
    @_abandon_on_failure
    def my_wave(self) -> dict[str, Any]:
        # Share the transition lane with next/previous. Repeated requests observe
        # the first completed start rather than independently replacing its track.
        with self._advance_lock:
            if not self._enabled():
                raise RuntimeError("Интеграция Яндекс Музыки отключена")
            if self._background_enabled():
                active = self._active_wave155()
                if active:
                    return active
            try:
                return self._my_wave_start155()
            except PlaybackSuperseded:
                return self._cancelled_playback_result()

    def _my_wave_start155(self) -> dict[str, Any]:
        with self._playback_lock:
            operation_serial = self._manual_transport_serial
        if not self._enabled():
            raise RuntimeError("Интеграция Яндекс Музыки отключена")

        if self._background_enabled():
            client = self._api_client()
            if client is not None:
                station = "user:onyourwave"
                with self._playback_lock:
                    self._wave_queue_generation += 1
                    self._wave_last_completed_id = ""
                last_exc: Exception | None = None
                playback_attempted = False
                for attempt in range(1, 4):
                    try:
                        with self._playback_lock:
                            current_id = _track_id(self._current_track_obj) if self._current_track_obj is not None else str(self._current_track_meta.get("id", "") or "")
                            history_avoid = self._history[-12:] if self.wave_strict_repeat_policy() else []
                            avoid_ids = {_track_id(item) for item in history_avoid if _track_id(item)}
                        wave = self.wave_tracks(queue_track_id=current_id, avoid_ids=avoid_ids, fresh_start=True, fast_start=True, target_count=1)
                        tracks, batch_id = list(wave.get("tracks") or []), str(wave.get("batch_id", "") or "")
                        if not tracks:
                            if wave.get('network_unavailable'):
                                raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.")
                            if wave.get('reason') == 'recent_tracks_filtered':
                                raise WaveUnavailable('Яндекс Музыка пока предлагает только недавно сыгранные треки. Попробуйте включить «Мою волну» немного позже.')
                            raise WaveUnavailable('Станция не вернула доступные новые треки. Причина сохранена в диагностике Моей волны.')
                        first = self._pop_sequence_track(tracks)
                        floor_ids, floor_fp = self._wave_playback_floor()
                        if _track_id(first) in floor_ids or _track_fingerprint(first) in floor_fp:
                            raise RuntimeError("Моя волна пока предлагает только недавно сыгранные треки")
                        playback_attempted = True
                        data = self._play_track_object(
                            first, expected_manual_serial=operation_serial, station=station, batch_id=batch_id, queue=tracks,
                            auto_advance=True, remember_previous=True, stream_first=True,
                        )
                        # fresh_start already sent radioStarted before fetching.
                        # A second start here would reset the continuation branch.
                        self._after_playback_effect("track_feedback", self._station_feedback, "trackStarted", _track_id(first))
                        self._after_playback_effect("queue_refill", self._ensure_wave_queue_depth, 8)
                        logger.info("Yandex My Wave started from Rotor station %s, batch=%s", station, batch_id or "<none>")
                        return {
                            **data, "wave": True, "station": station,
                            "station_kind": "my_wave",
                            "method": "yandex_rotor_onyourwave+jarvis_background_qt",
                        }
                    except PlaybackSuperseded:
                        raise
                    except Exception as exc:
                        self._assert_transport_current(operation_serial)
                        active = self._active_wave155()
                        if active:
                            logger.warning("Wave playing; post-start effect failed: %s", exc)
                            return active
                        last_exc = exc
                        logger.warning("Background My Wave attempt %s/3 failed: %s", attempt, exc)
                        if self._note_network_failure(exc):
                            raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.") from exc
                        if isinstance(exc, WaveUnavailable):
                            break  # The bounded real-context reads already exhausted this pool.
                        if playback_attempted:
                            break  # Ambiguous native start must never select/play another song.
                        if attempt < 3:
                            time.sleep(0.25 * attempt)
                # Preserve recovery from an unexpected resolver/metadata failure.
                # Exhausted pools and network faults are already classified above:
                # those must not trigger another identical blank-context request.
                if last_exc is not None and not playback_attempted and not isinstance(last_exc, WaveUnavailable):
                    try:
                        direct, batch = self._station_tracks(station, queue_track_id=current_id)
                        floor_ids, floor_fp = self._wave_playback_floor()
                        seen_ids, seen_fp = set(floor_ids) | {current_id}, set(floor_fp)
                        pending = []
                        for track in direct:
                            tid, fp = _track_id(track), _track_fingerprint(track)
                            available = track.get('available', True) if isinstance(track, dict) else getattr(track, 'available', True)
                            if not tid or available is False or tid in seen_ids or (fp and fp in seen_fp):
                                continue
                            pending.append(track)
                            seen_ids.add(tid)
                            if fp:
                                seen_fp.add(fp)
                        if pending:
                            first = self._pop_sequence_track(pending)
                            data = self._play_track_object(first, expected_manual_serial=operation_serial,
                                station=station, batch_id=batch, queue=pending, auto_advance=True,
                                remember_previous=True, stream_first=True)
                            self._after_playback_effect("track_feedback", self._station_feedback, "trackStarted", _track_id(first))
                            self._after_playback_effect("queue_refill", self._ensure_wave_queue_depth, 8)
                            return {**data, "wave": True, "station": station, "station_kind": "my_wave",
                                    "method": "yandex_rotor_direct_recovery"}
                    except PlaybackSuperseded:
                        raise
                    except Exception as recovery_exc:
                        if self._note_network_failure(recovery_exc):
                            raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.") from recovery_exc
                        logger.warning("Wave direct recovery failed: %s", type(recovery_exc).__name__)
                if self._strict_non_invasive():
                    if isinstance(last_exc, WaveUnavailable):
                        raise last_exc
                    raise RuntimeError(
                        "Не удалось включить «Мою волну». Проверьте подключение Яндекс Музыки и попробуйте ещё раз."
                    ) from last_exc
            elif self._strict_non_invasive():
                raise RuntimeError(self._api_unavailable_message("фоновой персональной станции «Моя волна»"))

        # Compatibility path for users who deliberately allow UI takeover.
        if self._strict_non_invasive():
            raise RuntimeError("Фоновая «Моя волна» недоступна, а UI fallback запрещён режимом защиты ввода")
        launch_yandex_music(wait_seconds=20.0)
        window = focus_yandex_music_window()
        if window is None:
            raise RuntimeError("Не удалось получить окно приложения Яндекс Музыка")
        self._dismiss_bad_route_dialog(window)
        try:
            before = music_app_status("yandex_music")
        except Exception:
            before = None
        ui = self._try_wave_ui(window, before)
        if ui is not None:
            return ui
        if self._protocol_fallback() and os.name == "nt":
            uri = "yandexmusic://play-vibe?seeds=user:onyourwave"
            try:
                os.startfile(uri)  # type: ignore[attr-defined]
            except Exception:
                try:
                    import ctypes
                    ctypes.windll.shell32.ShellExecuteW(None, "open", uri, None, None, 0)
                except Exception as exc:
                    raise RuntimeError("Не удалось тихо открыть yandexmusic:// protocol") from exc
            confirmed = self._wait_wave_playback(before, timeout=5.5)
            if confirmed is not None:
                return {**confirmed, "method": "yandexmusic_protocol_onyourwave", "verified": True}
        raise RuntimeError("Не удалось подтвердить запуск «Моей волны»")

    def _current_track(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        background = self._background_status()
        with self._playback_lock:
            obj = self._current_track_obj
            meta = dict(self._current_track_meta)
        if obj is not None and background is not None:
            status = {
                **background, "app": "yandex_music",
                "title": str(background.get("title", "") or meta.get("title", "")),
                "artist": str(background.get("artist", "") or meta.get("artist", "")),
            }
            return status, {**meta, "object": obj, "id": str(meta.get("id", "") or _track_id(obj))}
        status = music_app_status("yandex_music")
        title = str(status.get("title", "") or "").strip()
        artist = str(status.get("artist", "") or "").strip()
        return status, self.resolve_track(title, artist) if title else None

    def set_track_preference(self, track_id: str, preference: str) -> dict[str, Any]:
        """Set a preference for an explicit track through the authenticated local account.

        This entry point is intentionally independent of the local player. Discord can
        therefore like/dislike its current shared track for the user running THIS JARVIS
        instance, without forwarding the account mutation to the Discord host.
        """
        if not self._enabled():
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        client = self._api_client()
        if client is None:
            raise RuntimeError(self._api_unavailable_message())
        tid = str(track_id or "").strip()
        if not tid:
            raise RuntimeError("Не удалось определить ID текущего трека")
        pref = str(preference or "").strip().casefold()
        if pref not in {"like", "dislike", "unlike", "undislike"}:
            raise ValueError(f"Неизвестная музыкальная оценка: {preference}")
        method_name = {
            "like": "users_likes_tracks_add",
            "unlike": "users_likes_tracks_remove",
            "dislike": "users_dislikes_tracks_add",
            "undislike": "users_dislikes_tracks_remove",
        }[pref]
        method = getattr(client, method_name, None)
        if not callable(method):
            raise RuntimeError(f"У установленной версии yandex-music нет метода {method_name}")

        # A positive and a negative preference should not coexist. Opposite-state
        # removal is best-effort; the requested mutation remains authoritative.
        opposite = {"like": "users_dislikes_tracks_remove", "dislike": "users_likes_tracks_remove"}.get(pref)
        if opposite:
            remove = getattr(client, opposite, None)
            if callable(remove):
                for arg in (tid, [tid]):
                    try:
                        with self._api_lock:
                            remove(arg)
                        break
                    except Exception:
                        continue

        last_exc: Exception | None = None
        for arg in (tid, [tid]):
            try:
                with self._api_lock:
                    result = method(arg)
                return {
                    "app": "yandex_music", "track_id": tid, "preference": pref,
                    "api_result": bool(result) if result is not None else True,
                    "method": f"yandex_api_{pref}", "verified": True, "non_invasive": True,
                }
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Яндекс Музыка не подтвердила {pref} для текущего трека: {last_exc or 'unknown error'}")

    def _api_like(self, desired: bool) -> dict[str, Any] | None:
        client = self._api_client()
        if client is None:
            return None
        status, resolved = self._current_track()
        if not resolved or not resolved.get("id"):
            return None
        track_id = str(resolved["id"])
        method = getattr(client, "users_likes_tracks_add" if desired else "users_likes_tracks_remove", None)
        if not callable(method):
            return None
        attempts = [(track_id,), ([track_id],)]
        # Library/API revisions have used both a scalar and a list. Try both,
        # rejecting only after the real client reports the call cannot be made.
        last_exc: Exception | None = None
        for args in attempts:
            try:
                result = method(*args)
                return {
                    **status,
                    "track_id": track_id,
                    "api_result": bool(result) if result is not None else True,
                    "method": "yandex_api_like" if desired else "yandex_api_unlike",
                    "verified": True,
                }
            except Exception as exc:
                last_exc = exc
        if last_exc:
            logger.warning("Yandex API like action failed; UI fallback will be tried: %s", last_exc)
        return None

    def _ui_like(self, desired: bool) -> dict[str, Any]:
        if not self._ui_fallback():
            raise RuntimeError("API Яндекс Музыки недоступен, а UI fallback отключён")
        launch_yandex_music(wait_seconds=20.0)
        window = focus_yandex_music_window()
        if window is None:
            raise RuntimeError("Не вижу окно приложения Яндекс Музыка")
        unlike_words = ("убрать лайк", "снять лайк", "remove like", "unlike")
        like_words = ("мне нравится", "нравится", "лайк", "like", "добавить в любим")
        state_is_liked = bool(self._find_controls(window, unlike_words))
        if desired and state_is_liked:
            return {"app": "yandex_music", "method": "native_uia_like", "already_in_requested_state": True, "verified": True}
        if (not desired) and not state_is_liked and self._find_controls(window, like_words):
            return {"app": "yandex_music", "method": "native_uia_unlike", "already_in_requested_state": True, "verified": True}
        candidates = self._find_controls(
            window,
            like_words if desired else unlike_words,
            exclude=("убрать", "снять", "remove", "unlike") if desired else (),
        )
        for control in candidates[:10]:
            if self._invoke(control):
                time.sleep(0.25)
                after_liked = bool(self._find_controls(window, unlike_words))
                if after_liked == desired:
                    return {"app": "yandex_music", "method": "native_uia_like" if desired else "native_uia_unlike", "verified": True}

        # Yandex desktop exposes F as the like toggle. Use it only after reading a
        # known pre-state so we never blindly invert the user's preference.
        before_known = state_is_liked or bool(self._find_controls(window, like_words))
        if before_known:
            import pyautogui
            try:
                window.set_focus()
            except Exception:
                pass
            pyautogui.press("f")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                after_liked = bool(self._find_controls(window, unlike_words))
                if after_liked == desired:
                    return {"app": "yandex_music", "method": "native_hotkey_f", "verified": True}
                time.sleep(0.12)
        raise RuntimeError("Яндекс Музыка не предоставила надёжный доступ к кнопке лайка в desktop-приложении")

    def like(self, desired: bool = True) -> dict[str, Any]:
        if not self._enabled():
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        status, resolved = self._current_track()
        if not resolved or not resolved.get("id"):
            raise RuntimeError("Не удалось определить текущий трек Яндекс Музыки")
        preference = self.set_track_preference(str(resolved["id"]), "like" if desired else "unlike")
        return {**status, **preference, "title": str(status.get("title", "") or resolved.get("title", "")), "artist": str(status.get("artist", "") or resolved.get("artist", ""))}

    def dislike(self) -> dict[str, Any]:
        if not self._enabled():
            raise RuntimeError("Интеграция Яндекс Музыки отключена")
        status, resolved = self._current_track()
        if not resolved or not resolved.get("id"):
            raise RuntimeError("Не удалось определить текущий трек Яндекс Музыки")
        disliked_id = str(resolved["id"])
        disliked_title = str(status.get("title", "") or resolved.get("title", ""))
        disliked_artist = str(status.get("artist", "") or resolved.get("artist", ""))
        preference = self.set_track_preference(disliked_id, "dislike")
        advanced = self.transport("next")
        return {
            **advanced, "disliked": True, "disliked_track_id": disliked_id,
            "disliked_title": disliked_title, "disliked_artist": disliked_artist,
            "preference_verified": bool(preference.get("verified", False)),
            "verified": bool(preference.get("verified", False)) and bool(advanced.get("verified", False)),
            "method": "yandex_api_dislike_then_next", "non_invasive": True,
        }

    def execute(self, action: str, **kwargs: Any) -> dict[str, Any]:
        if str(action or "").strip().casefold() in {
            "play_pause", "play", "pause", "stop", "next", "previous", "status", "background_status",
            "queue_status", "queue_clear", "set_volume", "volume_up", "volume_down",
            "shuffle_on", "shuffle_off", "repeat_on", "repeat_track", "repeat_playlist", "repeat_off",
        }:
            owner = local_playback_session(local_player()).owner()
            if owner is not None and owner is not self:
                return owner.execute(action, **kwargs)
        try:
            data = self._execute(action, **kwargs)
        except PlaybackSuperseded:
            return self._cancelled_playback_result()
        if isinstance(data, dict) and not data.get("search_only"):
            provenance = self._after_playback_effect("provenance", self.playback_provenance) or {}
            # Queueing a YouTube entry does not change what is currently playing.
            # Its operation source and active source must therefore be separate.
            if str(action).casefold() == "youtube_url":
                provenance.update(content_provider="youtube", media_source="YouTube")
            data = {**data, **provenance}
        return data

    def _execute(self, action: str, **kwargs: Any) -> dict[str, Any]:
        action = str(action or "").strip().casefold()
        # Compatibility with common LLM aliases while remaining API-only.
        action = {
            "like_song": "like", "like_track": "like", "thumbs_up": "like",
            "dislike_song": "dislike", "dislike_track": "dislike", "thumbs_down": "dislike",
            "unlike_song": "unlike", "unlike_track": "unlike",
            "play_my_tracks": "my_tracks", "my_songs": "my_tracks",
            "play_wave": "my_wave", "play_my_wave": "my_wave", "wave": "my_wave",
            "play_song": "play_track", "play_music": "my_wave", "search_track": "search_track",
            "find_track": "search_track", "find_song": "search_track", "start_track": "play_track",
            "play_playlist": "playlist", "playlist_by_name": "playlist", "start_playlist": "playlist",
        }.get(action, action)
        if action == "status":
            return self.status()
        if action == "queue_status":
            return self.queue_status()
        if action == "queue_clear":
            return self.queue_clear()
        if action in {"play_pause", "play", "pause", "next", "previous", "stop"}:
            return self.transport(action)
        if action == "search_track":
            query = str(kwargs.get("query", "") or "").strip()
            if not query:
                raise ValueError("Track query is required")
            found = self.resolve_track(query, str(kwargs.get("artist", "") or ""))
            if not found:
                raise RuntimeError("Track not found")
            track = {key: found[key] for key in ("id", "title", "artist", "score") if key in found}
            return {**track, "matches": [track], "verified": True, "search_only": True,
                    "provider": "yandex_music", "playback_changed": False}
        if action == "play_track":
            return self.play_track(str(kwargs.get("query", "") or ""), str(kwargs.get("artist", "") or ""))
        if action == "youtube_url":
            return self.play_youtube_url(
                str(kwargs.get("query", "") or kwargs.get("url", "") or ""),
                queue=bool(kwargs.get("queue", False)), next_up=bool(kwargs.get("next_up", False)),
            )
        if action == "my_wave":
            return self.my_wave()
        if action == "playlist":
            return self.play_playlist(str(kwargs.get("playlist", "") or kwargs.get("query", "") or ""))
        if action == "my_tracks":
            return self.play_my_tracks()
        if action == "shuffle_on":
            return self.set_shuffle(True)
        if action == "shuffle_off":
            return self.set_shuffle(False)
        if action in {"repeat_on", "repeat_track"}:
            return self.set_repeat("track")
        if action == "repeat_playlist":
            return self.set_repeat("playlist")
        if action == "repeat_off":
            return self.set_repeat("off")
        if action == "like":
            return self.like(True)
        if action == "unlike":
            return self.like(False)
        if action == "dislike":
            return self.dislike()
        if action == "set_volume":
            return self.set_background_volume(float(kwargs.get("volume", self._background_volume_percent())))
        if action == "volume_up":
            return self.adjust_background_volume(int(kwargs.get("step", 10) or 10))
        if action == "volume_down":
            return self.adjust_background_volume(-int(kwargs.get("step", 10) or 10))
        if action == "api_status":
            return self.api_status()
        if action == "background_status":
            # HOTFIX125: quick commands call this path frequently. It must be a
            # purely local snapshot: never initialize/refresh the Yandex API just
            # to learn player volume/state. Account/API health remains api_status.
            try:
                player_status = local_player().status_if_running() or {"state": "stopped"}
            except Exception as exc:
                player_status = {"state": "unavailable", "error": str(exc)}
            with self._playback_lock:
                current_meta = dict(self._current_track_meta)
                owns_track = self._current_track_obj is not None or bool(current_meta)
                station = str(self._station or "")
                shuffle = bool(self._shuffle_enabled)
                repeat_mode = str(self._repeat_mode or "off")
            state = str(player_status.get("state", "stopped") or "stopped").casefold()
            try:
                volume_percent = int(round(float(player_status.get("volume", self._background_volume_percent() / 100.0)) * 100.0))
            except Exception:
                volume_percent = self._background_volume_percent()
            return {
                "enabled": self._enabled(), "provider": "yandex_music",
                "background_playback": True, "never_steal_input": True,
                "player": player_status, "state": state,
                "playing": state in {"playing", "buffering", "starting"},
                "paused": state == "paused",
                "owns_track": owns_track, "current": current_meta,
                "title": str(current_meta.get("title", "") or player_status.get("title", "") or ""),
                "artist": str(current_meta.get("artist", "") or player_status.get("artist", "") or ""),
                "media_source": str(current_meta.get("media_source", "") or "Yandex Music"),
                "station": station, "shuffle": shuffle, "repeat_mode": repeat_mode,
                "volume_percent": max(0, min(100, int(volume_percent))),
            }
        raise ValueError(
            f"Команда Яндекс Музыки «{action}» недоступна в API-only режиме. "
            "Desktop/UI управление удалено."
        )
