# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""YouTube URL -> JARVIS audio metadata/stream resolver.

HOTFIX79 routes an explicit URL to either the local hidden player or the shared
Discord player before LLM catalog routing and retries public YouTube media with
player clients that do not require account cookies when a selected media URL is
rejected with HTTP 403. It does not use browser cookies and
does not attempt to bypass private/account/DRM restrictions. Public and ordinary
unlisted-by-link videos/playlists can be resolved when YouTube exposes a normal
playback format to yt-dlp.
"""

import logging
import os
import re
import threading
import time
import weakref
from collections import OrderedDict
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from jarvis.runtime.youtube_runtime import ensure_js_runtime
from jarvis.integrations.media_state import cache_lock, MediaRequestSuperseded

logger = logging.getLogger(__name__)
_DOWNLOAD_SLOTS = threading.BoundedSemaphore(2)
_DOWNLOAD_FAILURES: OrderedDict[str, float] = OrderedDict()
_DOWNLOAD_FAILURE_LOCK = threading.Lock()
_DOWNLOAD_FAILURE_COOLDOWN = 15.0

_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
}
# Russian ASR/LLM output can preserve the URL shape while translating only the
# service hostname (observed in field logs as ``https://www.ютуб.com/watch?...``).
# Normalize only the hostname; path/query case is intentionally left untouched
# because YouTube video/playlist identifiers can be case-sensitive.
_HOST_ALIASES = {
    "ютуб.com": "youtube.com", "www.ютуб.com": "www.youtube.com",
    "ютьюб.com": "youtube.com", "www.ютьюб.com": "www.youtube.com",
    "ютюб.com": "youtube.com", "www.ютюб.com": "www.youtube.com",
    "уоутубе.com": "youtube.com", "www.уоутубе.com": "www.youtube.com",
}


def _clean_candidate(value: str) -> str:
    return str(value or "").strip().strip("<>[](){}\"'.,;!")


def normalize_youtube_url(value: str) -> str:
    """Return a canonical YouTube URL, including narrow RU-ASR host repair."""
    raw = _clean_candidate(value)
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ""
    host = str(parsed.hostname or "").casefold().rstrip(".")
    canonical_host = _HOST_ALIASES.get(host, host)
    if canonical_host not in _ALLOWED_HOSTS:
        return ""
    # Rebuild netloc without ever lower-casing or decoding the path/query.
    port = f":{parsed.port}" if parsed.port else ""
    netloc = canonical_host + port
    rebuilt = parsed._replace(netloc=netloc).geturl()
    return _clean_candidate(rebuilt)


def is_youtube_url(value: str) -> bool:
    raw = normalize_youtube_url(value)
    if not raw:
        return False
    parsed = urlparse(raw)
    host = str(parsed.hostname or "").casefold().rstrip(".")
    if host.endswith("youtu.be"):
        return bool(parsed.path.strip("/"))
    path = parsed.path.casefold()
    if path in {"/watch", "/playlist"}:
        return True
    return path.startswith(("/shorts/", "/live/", "/embed/"))


def extract_youtube_url(text: str) -> str:
    """Extract exactly one YouTube URL from a command while preserving its payload."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    valid: list[str] = []
    for candidate in _URL_RE.findall(raw):
        normalized = normalize_youtube_url(candidate)
        if normalized and is_youtube_url(normalized):
            valid.append(normalized)
    return valid[0] if len(valid) == 1 else ""


def standalone_youtube_url(text: str) -> str:
    """Return a URL only when the message is effectively just one YouTube link."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    matches = _URL_RE.findall(raw)
    if len(matches) != 1:
        return ""
    valid = normalize_youtube_url(matches[0])
    if not valid or not is_youtube_url(valid):
        return ""
    remainder = raw.replace(matches[0], "", 1)
    remainder = re.sub(r"[\s<>\[\](){}\"'.,;:!—–-]+", "", remainder)
    return valid if not remainder else ""


def explicit_playlist_url(url: str) -> bool:
    if not is_youtube_url(url):
        return False
    parsed = urlparse(_clean_candidate(url))
    host = str(parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    query = parse_qs(parsed.query)
    # A watch URL with &list= remains a single-video request. Only a real
    # /playlist URL expands the entire playlist, preventing accidental 500-item
    # queue additions when a user copies a video from inside a playlist.
    return host != "youtu.be" and path == "/playlist" and bool(query.get("list"))


def _video_id_hint(url: str) -> str:
    """Best-effort YouTube video id used only for a local cache lookup."""
    clean = normalize_youtube_url(url)
    if not clean:
        return ""
    parsed = urlparse(clean)
    host = str(parsed.hostname or "").casefold()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/", 1)[0].strip()
    path = parsed.path.casefold()
    if path == "/watch":
        return str((parse_qs(parsed.query).get("v") or [""])[0] or "").strip()
    if path.startswith(("/shorts/", "/live/", "/embed/")):
        parts = [part for part in parsed.path.split("/") if part]
        return str(parts[1] if len(parts) > 1 else "").strip()
    return ""


@dataclass(frozen=True)
class YouTubeItem:
    webpage_url: str
    video_id: str
    title: str
    artist: str
    duration_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "webpage_url": self.webpage_url,
            "video_id": self.video_id,
            "title": self.title,
            "artist": self.artist,
            "duration_ms": self.duration_ms,
        }


class YouTubeAudioResolver:
    def __init__(self, config: Any):
        self.config = config
        self._download_locks_guard = threading.RLock()
        # A strong dict kept one Lock forever for every YouTube URL ever seen.
        # Weak values preserve same-key synchronization while active callers hold
        # the lock, then let it disappear automatically after the download ends.
        self._download_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
        self._audio_cache_dir: Path | None = None
        self._audio_cache_key = ""
        self._js_runtime_cache: tuple[str, str] | None = None
        self._js_runtime_lock = threading.Lock()

    def _audio_cache_root(self) -> Path:
        configured = str(os.getenv("NEOJARVIS_CACHE_DIR", "") or "").strip()
        base = "" if configured else str(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home()))
        cache_key = configured or base
        cached = self._audio_cache_dir
        if cached is not None and self._audio_cache_key == cache_key:
            return cached
        if configured:
            root = Path(configured) / "youtube_audio"
        else:
            root = Path(base) / "NeoJarvis" / "cache" / "youtube_audio"
        root.mkdir(parents=True, exist_ok=True)
        self._audio_cache_dir = root
        self._audio_cache_key = cache_key
        return root

    def _download_lock(self, key: str) -> threading.Lock:
        normalized = str(key or "youtube").strip() or "youtube"
        with self._download_locks_guard:
            lock = self._download_locks.get(normalized)
            if lock is None:
                lock = threading.Lock()
                self._download_locks[normalized] = lock
            return lock

    @staticmethod
    def _is_ready_audio_file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size >= 4096 and not path.name.endswith((".part", ".ytdl"))
        except Exception:
            return False

    def _cached_audio_for_id(self, video_id: str) -> Path | None:
        video_id = re.sub(r"[^0-9A-Za-z_-]+", "", str(video_id or ""))
        if not video_id:
            return None
        candidates = [p for p in self._audio_cache_root().glob(video_id + ".*") if self._is_ready_audio_file(p)]
        if not candidates:
            return None
        try:
            return max(candidates, key=lambda p: p.stat().st_mtime)
        except Exception:
            return candidates[0]

    def _prune_audio_cache(self, *, keep: Path | None = None) -> None:
        """Keep the transient YouTube handoff bounded without touching active files."""
        root = self._audio_cache_root()
        try:
            files = [p for p in root.iterdir() if self._is_ready_audio_file(p)]
            total = sum(int(p.stat().st_size) for p in files)
            max_bytes = 1_500 * 1024 * 1024
            if total <= max_bytes:
                return
            keep_norm = os.path.normcase(os.path.abspath(str(keep))) if keep else ""
            files.sort(key=lambda p: p.stat().st_mtime)
            for path in files:
                if total <= 1_050 * 1024 * 1024:
                    break
                if keep_norm and os.path.normcase(os.path.abspath(str(path))) == keep_norm:
                    continue
                try:
                    size = int(path.stat().st_size)
                    path.unlink()
                    total -= size
                except Exception:
                    pass
        except Exception:
            pass

    def _base_options(self) -> dict[str, Any]:
        # Runtime discovery/repair can touch the filesystem and PATH. Cache a
        # healthy result per resolver instead of repeating it for every track.
        runtime = self._js_runtime_cache
        runtime_kind = ""
        runtime_path = ""
        if runtime is not None:
            runtime_kind, runtime_path = runtime
            if runtime_path and not Path(runtime_path).exists():
                runtime = None
        if runtime is None:
            with self._js_runtime_lock:
                runtime = self._js_runtime_cache
                if runtime is None or (runtime[1] and not Path(runtime[1]).exists()):
                    try:
                        runtime = ensure_js_runtime(progress=False)
                    except Exception as exc:
                        raise RuntimeError(
                            "YouTube audio runtime не удалось подготовить автоматически: " + str(exc)
                        ) from exc
                    self._js_runtime_cache = (str(runtime[0]), str(runtime[1]))
                runtime_kind, runtime_path = self._js_runtime_cache
        return {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 18,
            "retries": 3,
            "fragment_retries": 3,
            "js_runtimes": {runtime_kind: {"path": runtime_path}},
        }

    @staticmethod
    def _import_ydl() -> Any:
        try:
            from yt_dlp import YoutubeDL
            return YoutubeDL
        except Exception as exc:
            raise RuntimeError("Компонент yt-dlp не установлен; запустите восстановление зависимостей NEO") from exc

    @staticmethod
    def _item_from_info(info: dict[str, Any]) -> YouTubeItem:
        video_id = str(info.get("id", "") or "").strip()
        webpage_url = str(info.get("webpage_url", "") or info.get("original_url", "") or "").strip()
        if not webpage_url and video_id:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        title = str(info.get("title", "") or "YouTube video").strip()
        artist = str(
            info.get("artist", "") or info.get("creator", "") or info.get("uploader", "")
            or info.get("channel", "") or "YouTube"
        ).strip()
        duration = info.get("duration")
        try:
            duration_ms = max(0, int(float(duration or 0) * 1000.0))
        except Exception:
            duration_ms = 0
        return YouTubeItem(webpage_url, video_id, title, artist, duration_ms)

    def expand(self, url: str, *, limit: int = 200) -> dict[str, Any]:
        clean = normalize_youtube_url(url)
        if not clean or not is_youtube_url(clean):
            raise ValueError("Нужна прямая ссылка YouTube")
        YoutubeDL = self._import_ydl()
        if explicit_playlist_url(clean):
            options = {
                **self._base_options(),
                "extract_flat": "in_playlist",
                "playlistend": max(1, min(500, int(limit or 200))),
                "noplaylist": False,
            }
            try:
                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(clean, download=False)
            except Exception as exc:
                raise RuntimeError(self._friendly_error(exc)) from exc
            raw_entries = list((info or {}).get("entries") or [])
            items: list[YouTubeItem] = []
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                item = self._item_from_info(raw)
                if item.webpage_url and item.video_id:
                    items.append(item)
            if not items:
                raise RuntimeError("YouTube-плейлист не содержит доступных видео")
            return {
                "kind": "playlist",
                "title": str((info or {}).get("title", "") or "YouTube playlist").strip(),
                "items": [item.as_dict() for item in items],
                "truncated": len(raw_entries) >= int(options["playlistend"]),
            }

        options = {**self._base_options(), "noplaylist": True, "extract_flat": False}
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(clean, download=False)
        except Exception as exc:
            raise RuntimeError(self._friendly_error(exc)) from exc
        if not isinstance(info, dict):
            raise RuntimeError("YouTube не вернул информацию о видео")
        item = self._item_from_info(info)
        if not item.webpage_url:
            item = YouTubeItem(clean, item.video_id, item.title, item.artist, item.duration_ms)
        return {"kind": "video", "title": item.title, "items": [item.as_dict()], "truncated": False}

    def resolve_audio(self, url: str) -> dict[str, Any]:
        clean = normalize_youtube_url(url)
        if not clean or not is_youtube_url(clean):
            raise ValueError("Нужна ссылка YouTube")
        YoutubeDL = self._import_ydl()
        options = {
            **self._base_options(),
            "noplaylist": True,
            "format": "bestaudio[acodec!=none]/bestaudio/best",
        }
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(clean, download=False)
        except Exception as exc:
            raise RuntimeError(self._friendly_error(exc)) from exc
        if not isinstance(info, dict):
            raise RuntimeError("YouTube не вернул аудиопоток")
        stream_url = str(info.get("url", "") or "").strip()
        if not stream_url:
            requested = list(info.get("requested_formats") or [])
            audio = next((x for x in requested if isinstance(x, dict) and str(x.get("acodec", "none")) != "none" and x.get("url")), None)
            if audio:
                stream_url = str(audio.get("url", "") or "").strip()
        if not stream_url:
            raise RuntimeError("Для этого YouTube-видео не найден доступный аудиопоток")
        item = self._item_from_info(info)
        headers = info.get("http_headers") if isinstance(info.get("http_headers"), dict) else {}
        return {
            **item.as_dict(),
            "stream_url": stream_url,
            "http_headers": {str(k): str(v) for k, v in dict(headers or {}).items() if v is not None},
            "format_id": str(info.get("format_id", "") or ""),
            "ext": str(info.get("ext", "") or ""),
        }

    @staticmethod
    def _is_http_403_error(exc: BaseException) -> bool:
        text = str(exc or "").casefold()
        return "403" in text and ("forbidden" in text or "http error 403" in text)

    def _download_profiles(self) -> list[tuple[str, dict[str, Any]]]:
        """Ordered public-video download profiles.

        YouTube can expose a format URL that later rejects the media request with
        HTTP 403 when that player client requires a GVS PO token.  We do not import
        browser cookies or account credentials.  Instead, after the normal yt-dlp
        profile we retry clients/formats that yt-dlp documents as usable without
        account cookies.  web_safari HLS is preferred first because HLS playback
        is currently exempt from GVS PO-token enforcement for that client.
        """
        common = {
            "noplaylist": True,
            "continuedl": True,
            "overwrites": True,
            "nopart": False,
        }
        return [
            (
                "default_audio",
                {
                    **common,
                    "format": "bestaudio[acodec!=none][vcodec=none]/bestaudio[acodec!=none]/best[acodec!=none]",
                    "concurrent_fragment_downloads": 4,
                },
            ),
            (
                "web_safari_hls",
                {
                    **common,
                    "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
                    "format": (
                        "bestaudio[protocol*=m3u8][acodec!=none]/"
                        "best[protocol*=m3u8][height<=360][acodec!=none]/"
                        "worst[protocol*=m3u8][acodec!=none]"
                    ),
                    "concurrent_fragment_downloads": 6,
                },
            ),
            (
                "web_embedded",
                {
                    **common,
                    "extractor_args": {"youtube": {"player_client": ["web_embedded"]}},
                    "format": "bestaudio[acodec!=none]/best[height<=360][acodec!=none]/best[acodec!=none]",
                    "concurrent_fragment_downloads": 4,
                },
            ),
            (
                "android_vr",
                {
                    **common,
                    "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
                    "format": "bestaudio[acodec!=none]/best[height<=360][acodec!=none]/best[acodec!=none]",
                    "concurrent_fragment_downloads": 4,
                },
            ),
            (
                "tv",
                {
                    **common,
                    "extractor_args": {"youtube": {"player_client": ["tv"]}},
                    "format": "bestaudio[acodec!=none]/best[height<=360][acodec!=none]/best[acodec!=none]",
                    "concurrent_fragment_downloads": 4,
                },
            ),
        ]

    @staticmethod
    def _purge_partial_downloads(root: Path, video_id: str) -> None:
        safe_id = re.sub(r"[^0-9A-Za-z_-]+", "", str(video_id or ""))
        # Never remove the other bounded worker's in-progress transfer.
        patterns = [safe_id + ".*"] if safe_id else []
        seen: set[str] = set()
        for pattern in patterns:
            try:
                for path in root.glob(pattern):
                    key = str(path)
                    if key in seen:
                        continue
                    seen.add(key)
                    if not path.is_file():
                        continue
                    # Do not remove a completed cache file from a previous attempt.
                    if path.suffix.casefold() not in {".part", ".ytdl"} and not path.name.endswith((".part", ".ytdl")):
                        continue
                    try:
                        path.unlink()
                    except Exception:
                        pass
            except Exception:
                pass

    def download_audio(self, url: str, *, check_cancelled: Callable[[], None] | None = None) -> dict[str, Any]:
        key = normalize_youtube_url(url)
        identity = _video_id_hint(key) or key
        lock = cache_lock("youtube:" + identity)
        def acquire(resource: Any) -> bool:
            if check_cancelled is None:
                return resource.acquire(timeout=20.0)
            deadline = time.monotonic() + 20.0
            while True:
                check_cancelled()
                if resource.acquire(timeout=0.05):
                    return True
                if time.monotonic() >= deadline:
                    return False
        if not acquire(lock):
            raise TimeoutError("Подготовка этого трека уже выполняется. Выберите другой трек или повторите позже")
        try:
            with _DOWNLOAD_FAILURE_LOCK:
                failed_at = _DOWNLOAD_FAILURES.get(identity)
                if failed_at is not None and time.monotonic() - failed_at < _DOWNLOAD_FAILURE_COOLDOWN:
                    raise ConnectionError("YouTube недавно не предоставил этот трек. Повторите через 15 секунд или выберите другой")
            if not acquire(_DOWNLOAD_SLOTS):
                raise TimeoutError("Очередь подготовки YouTube занята. Повторите через несколько секунд.")
            try:
                if check_cancelled is not None:
                    result = self._download_audio_bounded(url, check_cancelled=check_cancelled)
                else:
                    result = self._download_audio_bounded(url)
            except MediaRequestSuperseded:
                raise
            except Exception:
                with _DOWNLOAD_FAILURE_LOCK:
                    _DOWNLOAD_FAILURES[identity] = time.monotonic()
                    _DOWNLOAD_FAILURES.move_to_end(identity)
                    while len(_DOWNLOAD_FAILURES) > 128:
                        _DOWNLOAD_FAILURES.popitem(last=False)
                raise
            else:
                with _DOWNLOAD_FAILURE_LOCK:
                    _DOWNLOAD_FAILURES.pop(identity, None)
                return result
            finally:
                _DOWNLOAD_SLOTS.release()
        finally:
            lock.release()

    def _download_audio_bounded(self, url: str, *, check_cancelled: Callable[[], None] | None = None) -> dict[str, Any]:
        """Materialise one YouTube audio stream into JARVIS' managed cache.

        Passing a raw googlevideo URL from yt-dlp directly to another HTTP
        client is fragile: the URL can depend on downloader-specific headers,
        protocol options and chunking.  HOTFIX79 lets yt-dlp perform the media
        transfer itself, then hands a normal local file to FFmpeg/Qt.
        """
        if check_cancelled is not None:
            check_cancelled()
        clean = normalize_youtube_url(url)
        if not clean or not is_youtube_url(clean):
            raise ValueError("Нужна ссылка YouTube")

        hinted_id = _video_id_hint(clean)
        cached = self._cached_audio_for_id(hinted_id)
        if cached is not None:
            try:
                os.utime(cached, None)
            except Exception:
                pass
            return {
                "webpage_url": clean,
                "video_id": hinted_id,
                "title": "",
                "artist": "",
                "duration_ms": 0,
                "local_path": str(cached),
                "source_method": "youtube_ytdlp_cached_audio_hit",
            }

        lock = self._download_lock(hinted_id or clean)
        with lock:
            # A parallel queue prefetch may have completed while we waited.
            cached = self._cached_audio_for_id(hinted_id)
            if cached is not None:
                return {
                    "webpage_url": clean,
                    "video_id": hinted_id,
                    "title": "",
                    "artist": "",
                    "duration_ms": 0,
                    "local_path": str(cached),
                    "source_method": "youtube_ytdlp_cached_audio_hit",
                }

            YoutubeDL = self._import_ydl()
            root = self._audio_cache_root()
            base_options = self._base_options()
            info: dict[str, Any] | None = None
            prepared_name = ""
            profile_used = ""
            failures: list[tuple[str, str]] = []
            deadline = time.monotonic() + 90

            def check_budget(_progress: Any) -> None:
                if check_cancelled is not None:
                    check_cancelled()
                if time.monotonic() > deadline:
                    raise TimeoutError("YouTube audio preparation exceeded its 90 second budget")

            for profile_name, profile_options in self._download_profiles():
                check_budget(None)
                options = {
                    **base_options,
                    **profile_options,
                    "skip_download": False,
                    "outtmpl": str(root / "%(id)s.%(ext)s"),
                    "concurrent_fragment_downloads": 2,
                    "socket_timeout": 12,
                    "retries": 1,
                    "fragment_retries": 1,
                    "progress_hooks": [check_budget],
                }
                try:
                    logger.info("YouTube audio download attempt profile=%s url=%s", profile_name, clean)
                    with YoutubeDL(options) as ydl:
                        candidate_info = ydl.extract_info(clean, download=True)
                        candidate_name = str(ydl.prepare_filename(candidate_info) if isinstance(candidate_info, dict) else "")
                    check_budget(None)
                    if not isinstance(candidate_info, dict):
                        raise RuntimeError("YouTube не вернул скачанный аудиофайл")
                    info = candidate_info
                    prepared_name = candidate_name
                    profile_used = profile_name
                    logger.info("YouTube audio download profile succeeded: %s", profile_name)
                    break
                except MediaRequestSuperseded:
                    self._purge_partial_downloads(root, hinted_id)
                    raise
                except Exception as exc:
                    if check_cancelled is not None:
                        check_cancelled()
                    failures.append((profile_name, str(exc or "").strip()))
                    logger.warning("YouTube audio download profile failed: %s: %s", profile_name, exc)
                    self._purge_partial_downloads(root, hinted_id)
                    # A non-403 error may still be client-specific (format unavailable,
                    # embeddability, Kids restriction), so continue through the public
                    # fallback profiles. Authentication/private-video errors are mapped
                    # later to a clear message without ever importing browser cookies.
                    continue

            if not isinstance(info, dict):
                joined = " | ".join(f"{name}: {msg}" for name, msg in failures if msg)
                last_text = failures[-1][1] if failures else "unknown error"
                if any("403" in msg and "forbidden" in msg.casefold() for _, msg in failures):
                    raise RuntimeError(
                        "Не удалось получить аудио YouTube после автоматических 403-fallback попыток. "
                        "YouTube отклонил media URL всех доступных публичных клиентов. "
                        + (joined[-1200:] if joined else last_text)
                    )
                raise RuntimeError(self._friendly_error(RuntimeError(last_text)))

            item = self._item_from_info(info)
            candidates: list[Path] = []
            requested = list(info.get("requested_downloads") or [])
            for raw in requested:
                if not isinstance(raw, dict):
                    continue
                for key in ("filepath", "filename", "_filename"):
                    value = str(raw.get(key, "") or "").strip()
                    if value:
                        candidates.append(Path(value))
            for key in ("filepath", "_filename"):
                value = str(info.get(key, "") or "").strip()
                if value:
                    candidates.append(Path(value))
            if prepared_name:
                candidates.append(Path(prepared_name))
            if item.video_id:
                candidates.extend(root.glob(re.sub(r"[^0-9A-Za-z_-]+", "", item.video_id) + ".*"))

            local_path: Path | None = None
            root_norm = os.path.normcase(os.path.abspath(str(root)))
            for candidate in candidates:
                try:
                    candidate = candidate.resolve(strict=False)
                    candidate_norm = os.path.normcase(os.path.abspath(str(candidate)))
                    if os.path.commonpath([root_norm, candidate_norm]) != root_norm:
                        continue
                    if self._is_ready_audio_file(candidate):
                        local_path = candidate
                        break
                except Exception:
                    continue
            if local_path is None:
                raise RuntimeError("yt-dlp завершил загрузку, но локальный аудиофайл YouTube не найден")

            self._prune_audio_cache(keep=local_path)
            return {
                **item.as_dict(),
                "local_path": str(local_path),
                "source_method": "youtube_ytdlp_cached_audio",
                "download_profile": profile_used,
                "format_id": str(info.get("format_id", "") or ""),
                "ext": str(info.get("ext", "") or local_path.suffix.lstrip(".")),
            }

    @staticmethod
    def _friendly_error(exc: BaseException) -> str:
        text = str(exc or "").strip()
        low = text.casefold()
        if "private video" in low or "this video is private" in low:
            return "Это приватное YouTube-видео; одной ссылки недостаточно для доступа"
        if "sign in" in low or "not a bot" in low or "confirm your age" in low or "age-restricted" in low:
            return (
                "Discord Music не требует YouTube API/OAuth для прямой ссылки, но YouTube отклонил публичный "
                "медиа-транспорт этой ссылки проверкой anti-bot. NEO не использует cookies аккаунта; "
                "попробуйте обычное публичное видео или повторите попытку позже."
            )
        if "video unavailable" in low or "unavailable" in low:
            return "YouTube-видео недоступно из текущей сети или было удалено"
        if "403" in low and "forbidden" in low:
            return (
                "YouTube отклонил выбранный media URL (HTTP 403). NEO автоматически попробует "
                "альтернативные публичные player-client/HLS профили при загрузке аудио."
            )
        return f"Не удалось получить аудио YouTube: {text or 'unknown error'}"
