# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import re
from typing import Any

import requests
from jarvis.secrets import unprotect_secret


class YouTubeAPIError(RuntimeError):
    pass


class YouTubeAPIAdapter:
    ENDPOINT = "https://www.googleapis.com/youtube/v3/search"

    def __init__(self, config: Any):
        self.config = config
        self.session = requests.Session()
        self._api_key_cache: tuple[str, str] = ("", "")

    def _key(self) -> str:
        encrypted = str(self.config.get("integrations.youtube.api_key_encrypted", "") or "")
        source, cached = self._api_key_cache
        if source == encrypted:
            return cached
        value = str(unprotect_secret(encrypted) if encrypted else "").strip()
        self._api_key_cache = (encrypted, value)
        return value

    @staticmethod
    def _video_id_from_url(value: str) -> str:
        text = str(value or "").strip()
        patterns = (
            r"youtu\.be/([A-Za-z0-9_-]{6,})",
            r"youtube\.com/watch\?[^\s]*v=([A-Za-z0-9_-]{6,})",
            r"youtube\.com/shorts/([A-Za-z0-9_-]{6,})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return match.group(1)
        return ""

    def search_video(self, query: str, author: str = "") -> dict[str, Any]:
        query = str(query or "").strip()
        author = str(author or "").strip()
        direct_id = self._video_id_from_url(query)
        if direct_id:
            return {"video_id": direct_id, "title": "YouTube", "channel": "", "url": f"https://www.youtube.com/watch?v={direct_id}", "verified": True}
        key = self._key()
        if not key:
            raise YouTubeAPIError("YouTube API key не настроен. Включите YouTube в Настройки → Медиа и укажите API key.")
        search_query = " ".join(part for part in (query, author) if part).strip()
        response = self.session.get(
            self.ENDPOINT,
            params={"part": "snippet", "type": "video", "maxResults": 5 if author else 1, "q": search_query, "key": key, "safeSearch": "none"},
            timeout=15,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            err = payload.get("error") if isinstance(payload, dict) else {}
            message = str((err or {}).get("message", "") if isinstance(err, dict) else err or "")
            raise YouTubeAPIError(message or f"YouTube Data API HTTP {response.status_code}")
        items = payload.get("items") if isinstance(payload, dict) else []
        if not items:
            raise YouTubeAPIError(f"YouTube не нашёл видео «{query}»")
        item = items[0]
        if author:
            want = re.sub(r"[^a-zа-я0-9]+", " ", author.casefold().replace("ё", "е")).strip()
            def _score(candidate: dict[str, Any]) -> float:
                snippet = candidate.get("snippet") or {}
                channel = re.sub(r"[^a-zа-я0-9]+", " ", str(snippet.get("channelTitle", "") or "").casefold().replace("ё", "е")).strip()
                title = re.sub(r"[^a-zа-я0-9]+", " ", str(snippet.get("title", "") or "").casefold().replace("ё", "е")).strip()
                score = 0.0
                if want and channel == want: score += 5.0
                if want and want in channel: score += 3.0
                if want and want in title: score += 1.0
                return score
            item = max(items, key=_score)
        video_id = str((item.get("id") or {}).get("videoId", "") or "")
        snippet = item.get("snippet") or {}
        if not video_id:
            raise YouTubeAPIError("YouTube Data API не вернул videoId")
        return {
            "video_id": video_id,
            "title": str(snippet.get("title", "") or ""),
            "channel": str(snippet.get("channelTitle", "") or ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "verified": True,
            "requested_author": author,
        }
