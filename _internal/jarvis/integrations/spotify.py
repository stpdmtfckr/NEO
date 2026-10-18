# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests

from jarvis.secrets import protect_secret, unprotect_secret
from jarvis.integrations.media_state import PlaybackSessionStore, serialized_transport

SPOTIFY_SCOPES = (
    "user-read-playback-state user-read-currently-playing user-modify-playback-state "
    "playlist-read-private user-library-read user-library-modify"
)


class SpotifyAPIError(RuntimeError):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def authorize_spotify_pkce(client_id: str, redirect_uri: str, *, timeout_seconds: float = 180.0) -> dict[str, Any]:
    client_id = str(client_id or "").strip()
    redirect_uri = str(redirect_uri or "http://127.0.0.1:8765/callback").strip()
    if not client_id:
        raise ValueError("Укажите Spotify Client ID")
    parsed = urllib.parse.urlsplit(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise ValueError("Для desktop PKCE используйте redirect URI вида http://127.0.0.1:8765/callback")
    verifier = _b64url(secrets.token_bytes(64))[:96]
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(24)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "scope": SPOTIFY_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "show_dialog": "true",
    })
    result: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            nonlocal result
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = {k: (v[0] if v else "") for k, v in query.items()}
            body = (
                "<html><meta charset='utf-8'><body style='font-family:Segoe UI;padding:32px'>"
                "<h2>NEO · Spotify</h2><p>Авторизация получена. Можно вернуться в NEO.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", int(parsed.port)), Handler)
    server.timeout = max(1.0, float(timeout_seconds))
    webbrowser.open(auth_url)
    started = time.monotonic()
    while not result and time.monotonic() - started < float(timeout_seconds):
        server.timeout = min(1.0, max(0.1, float(timeout_seconds) - (time.monotonic() - started)))
        server.handle_request()
    server.server_close()
    if not result:
        raise TimeoutError("Spotify не вернул OAuth callback за отведённое время")
    if result.get("state") != state:
        raise SpotifyAPIError("Spotify OAuth state не совпал")
    if result.get("error"):
        raise SpotifyAPIError(f"Spotify OAuth: {result.get('error')}")
    code = str(result.get("code", "") or "")
    if not code:
        raise SpotifyAPIError("Spotify не вернул authorization code")
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    payload = response.json() if response.content else {}
    if response.status_code >= 400:
        raise SpotifyAPIError(str(payload.get("error_description") or payload.get("error") or f"HTTP {response.status_code}"))
    payload["obtained_at"] = time.time()
    payload["expires_at"] = time.time() + float(payload.get("expires_in", 3600) or 3600) - 30.0
    return payload


class SpotifyAdapter:
    API = "https://api.spotify.com/v1"

    def __init__(self, config: Any):
        self.config = config
        from jarvis.runtime import http_pool173
        self.session = http_pool173.session("spotify")      # NEO 173: shared, warmed up at the wake word
        self._secret_cache: dict[str, tuple[str, str]] = {}
        # HOTFIX125: background/quick controls may query state several times in a
        # few seconds. Keep a tiny in-process snapshot to avoid redundant Spotify
        # Web API round-trips on every no-wake volume/repeat/shuffle command.
        self._status_cache: dict[str, Any] = {}
        self._status_cache_at = 0.0
        self._playback_store = PlaybackSessionStore(config, "spotify")
        saved = self._playback_store.load()
        self._previous = list(saved.get("previous") or [])[-5:]
        self._confirmed_current = dict(saved.get("current") or {})
        self._navigation = ""

    def _secret(self, key: str) -> str:
        encrypted = str(self.config.get(key, "") or "")
        cached = self._secret_cache.get(key)
        if cached is not None and cached[0] == encrypted:
            return cached[1]
        value = str(unprotect_secret(encrypted) if encrypted else "").strip()
        self._secret_cache[key] = (encrypted, value)
        return value

    def _client_id(self) -> str:
        return self._secret("integrations.spotify.client_id_encrypted")

    def _access_token(self) -> str:
        return self._secret("integrations.spotify.access_token_encrypted")

    def _refresh_token(self) -> str:
        return self._secret("integrations.spotify.refresh_token_encrypted")

    def _refresh_if_needed(self, *, force: bool = False) -> str:
        token = self._access_token()
        expires = float(self.config.get("integrations.spotify.token_expires_at", 0.0) or 0.0)
        if not force and token and expires > time.time() + 25:
            return token
        refresh = self._refresh_token()
        client_id = self._client_id()
        generation = self._credentials_fingerprint()      # NEO 183: the connection this refresh belongs to
        if not refresh or not client_id:
            if token:
                return token
            raise SpotifyAPIError("Spotify не авторизован. Нажмите «Подключить Spotify» в Настройки → Медиа → Spotify.")
        response = self.session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise SpotifyAPIError(str(payload.get("error_description") or payload.get("error") or f"HTTP {response.status_code}"))
        access = str(payload.get("access_token", "") or "")
        expires_at = time.time() + float(payload.get("expires_in", 3600) or 3600) - 30
        rotated_refresh = str(payload.get("refresh_token", "") or refresh).strip()
        # NEO 183: while this request was on the way the user may have connected another account.
        # The answer belongs to the old connection: it is neither saved nor used.
        def stale(data: dict | None = None) -> bool:
            if data is None:
                return self._credentials_fingerprint() != generation
            node = (data.get("integrations") or {}).get("spotify") or {}
            return "|".join(str(node.get(k, "") or "") for k in ("refresh_token_encrypted", "client_id_encrypted")) != generation
        # Spotify may rotate refresh tokens. Persist the newest token and access
        # token through ConfigManager/DPAPI so authorization survives a restart.
        saved = None
        try:
            updater = getattr(self.config, "update", None)
            if callable(updater):
                def apply(data: dict) -> bool:
                    if stale(data):
                        return False
                    node = data.setdefault("integrations", {}).setdefault("spotify", {})
                    node["access_token_encrypted"] = protect_secret(access)
                    node["refresh_token_encrypted"] = protect_secret(rotated_refresh)
                    node["token_expires_at"] = float(expires_at)
                    return True
                saved = bool(updater(apply, credentials=True))
            else:
                raw = getattr(self.config, "data", None)
                saver = getattr(self.config, "save", None)
                if isinstance(raw, dict) and callable(saver) and not stale():
                    import json as _json
                    updated = _json.loads(_json.dumps(raw))
                    node = updated.setdefault("integrations", {}).setdefault("spotify", {})
                    node["access_token_encrypted"] = protect_secret(access)
                    node["refresh_token_encrypted"] = protect_secret(rotated_refresh)
                    node["token_expires_at"] = float(expires_at)
                    try:
                        saver(updated, credentials=True)
                    except TypeError:
                        saver(updated)
                    saved = True
        except Exception:
            # Runtime token remains valid even if durable persistence is blocked
            # (for example while a settings file is momentarily locked).
            saved = None
        if saved is False or (saved is None and stale()):
            self._runtime_access_token = ""
            self._runtime_expires_at = 0.0
            raise SpotifyAPIError("Подключение Spotify изменилось во время обновления токена. Повторите команду.")
        self._runtime_access_token = access
        self._runtime_expires_at = expires_at
        self._runtime_credentials = self._credentials_fingerprint()
        return self._runtime_access_token

    def _credentials_fingerprint(self) -> str:
        """Which saved connection the runtime token belongs to (NEO 182)."""
        return "|".join(str(self.config.get(key, "") or "") for key in (
            "integrations.spotify.refresh_token_encrypted", "integrations.spotify.client_id_encrypted"))

    def _token(self) -> str:
        runtime = str(getattr(self, "_runtime_access_token", "") or "")
        if runtime and getattr(self, "_runtime_credentials", None) != self._credentials_fingerprint():
            # NEO 182: the user connected another account (or disconnected): never keep using the
            # previous account's access token from memory.
            self._runtime_access_token = ""
            self._runtime_expires_at = 0.0
            runtime = ""
            for cache in ("_status_cache", "_device_cache", "_state_cache", "_playback_cache"):
                if isinstance(getattr(self, cache, None), dict):
                    getattr(self, cache).clear()
            self._status_cache_at = 0.0
        if runtime and float(getattr(self, "_runtime_expires_at", 0.0) or 0.0) > time.time() + 10:
            return runtime
        return self._refresh_if_needed()

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None, allow_204: bool = True) -> Any:
        from jarvis.runtime import http_pool173
        with http_pool173.timed("spotify", method.upper()):
            return self._request_timed(method, path, params=params, json_body=json_body, allow_204=allow_204)

    def _request_timed(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None, allow_204: bool = True) -> Any:
        response = self.session.request(
            method, self.API + path,
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
            params=params, json=json_body, timeout=15,
        )
        if response.status_code == 401:
            # An explicit 401 rejected the action; it is safe to refresh once.
            # Never retry a timed-out POST: next/queue may already have happened.
            refreshed = self._refresh_if_needed(force=True)
            response = self.session.request(
                method, self.API + path,
                headers={"Authorization": f"Bearer {refreshed}", "Content-Type": "application/json"},
                params=params, json=json_body, timeout=15,
            )
        if response.status_code == 429:
            wait = response.headers.get("Retry-After", "?")
            raise SpotifyAPIError(f"Spotify ограничил частоту запросов. Повторите через {wait} с; команда не повторялась автоматически.")
        if response.status_code == 204 and allow_204:
            return None
        payload: Any = {}
        if response.content:
            try:
                payload = response.json()
            except Exception:
                payload = {"text": response.text[:500]}
        if response.status_code >= 400:
            message = ""
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message", "") or "")
                else:
                    message = str(error or "")
            if response.status_code == 403 and not message:
                message = "Spotify playback требует Premium и разрешение user-modify-playback-state"
            raise SpotifyAPIError(message or f"Spotify HTTP {response.status_code}")
        return payload

    def on_network_changed(self, *_args: Any) -> None:
        previous = self.session
        from jarvis.runtime import http_pool173
        self.session = http_pool173.reset("spotify")
        self._status_cache_at = 0.0
        previous.close()

    @staticmethod
    def _track_payload(item: dict[str, Any] | None) -> dict[str, Any]:
        item = item or {}
        artists = item.get("artists") if isinstance(item.get("artists"), list) else []
        return {
            "track_id": str(item.get("id", "") or ""),
            "uri": str(item.get("uri", "") or ""),
            "title": str(item.get("name", "") or ""),
            "artist": ", ".join(str(x.get("name", "") or "") for x in artists if isinstance(x, dict) and x.get("name")),
            "duration_ms": int(item.get("duration_ms", 0) or 0),
        }

    def queue_status(self, *, limit: int = 5) -> dict[str, Any]:
        """Read Spotify's actual queue; never infer recommendations or mutate play."""
        data = self._request("GET", "/me/player/queue")
        if not isinstance(data, dict):
            raise SpotifyAPIError("Spotify не вернул очередь воспроизведения")
        current = data.get("currently_playing")
        current = self._track_payload(current) if isinstance(current, dict) else {}
        queue = data.get("queue")
        if not isinstance(queue, list):
            raise SpotifyAPIError("Spotify не вернул список следующих треков")
        if any(not isinstance(item, dict) for item in queue):
            raise SpotifyAPIError("Spotify вернул повреждённую очередь")
        rows = [self._track_payload(item) for item in queue[:max(1,min(50,int(limit)))]]
        return {"provider":"spotify", "verified":True, "current":current,
                "queue":rows, "upcoming":rows, "queue_available":True,
                "playback_changed":False}

    def status(self, *, allow_cached: bool = False, max_age_seconds: float = 4.0) -> dict[str, Any]:
        if allow_cached and self._status_cache and (time.monotonic() - self._status_cache_at) <= max(0.1, float(max_age_seconds)):
            return dict(self._status_cache)
        data = self._request("GET", "/me/player")
        if not data:
            payload = {"verified": True, "state": "stopped", "playing": False, "provider": "spotify"}
            self._status_cache = dict(payload)
            self._status_cache_at = time.monotonic()
            return payload
        item = data.get("item") if isinstance(data, dict) and isinstance(data.get("item"), dict) else {}
        payload = self._track_payload(item)
        payload.update({
            "verified": True,
            "provider": "spotify",
            "playing": bool(data.get("is_playing")),
            "paused": not bool(data.get("is_playing")) and bool(item),
            "state": "playing" if bool(data.get("is_playing")) else ("paused" if item else "stopped"),
            "position_ms": int(data.get("progress_ms", 0) or 0),
            "device": dict(data.get("device") or {}),
            "volume": int((data.get("device") or {}).get("volume_percent", 0) or 0),
            "shuffle": bool(data.get("shuffle_state", False)),
            "repeat_mode": {"context": "playlist", "track": "track", "off": "off"}.get(str(data.get("repeat_state", "off") or "off"), "off"),
        })
        if payload.get("playing") and payload.get("track_id"):
            observed = {k: payload[k] for k in ("track_id", "uri", "title", "artist", "duration_ms")}
            observed["provider"] = "spotify"
            if observed["track_id"] != self._confirmed_current.get("track_id"):
                if self._navigation == "previous" and self._previous and self._previous[-1].get("track_id") == observed["track_id"]:
                    self._previous.pop()
                elif self._confirmed_current:
                    self._previous = (self._previous + [self._confirmed_current])[-5:]
                observed["played_at"] = time.time()
                self._confirmed_current = observed
                self._playback_store.save({"current": observed, "previous": self._previous})
            payload["history"] = [dict(x) for x in self._previous]
        self._status_cache = dict(payload)
        self._status_cache_at = time.monotonic()
        return payload

    def devices(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/me/player/devices") or {}
        return [dict(x) for x in (data.get("devices") or []) if isinstance(x, dict)]

    def _choose_device(self) -> str:
        devices = self.devices()
        active = next((x for x in devices if x.get("is_active") and x.get("id")), None)
        chosen = active or next((x for x in devices if x.get("id") and not x.get("is_restricted")), None)
        if not chosen:
            raise SpotifyAPIError("Spotify не видит доступного Spotify Connect устройства. Запустите Spotify на компьютере или телефоне.")
        device_id = str(chosen.get("id") or "")
        if not chosen.get("is_active"):
            self._request("PUT", "/me/player", json_body={"device_ids": [device_id], "play": False})
            time.sleep(0.25)
        return device_id

    def search_track(self, query: str) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("Не указан трек Spotify")
        data = self._request("GET", "/search", params={"q": query, "type": "track", "limit": 1}) or {}
        items = ((data.get("tracks") or {}).get("items") or []) if isinstance(data, dict) else []
        if not items:
            raise SpotifyAPIError(f"Spotify не нашёл трек «{query}»")
        return self._track_payload(items[0])

    def search_playlist(self, query: str) -> dict[str, Any]:
        data = self._request("GET", "/search", params={"q": str(query or "").strip(), "type": "playlist", "limit": 1}) or {}
        items = ((data.get("playlists") or {}).get("items") or []) if isinstance(data, dict) else []
        item = next((x for x in items if isinstance(x, dict)), None)
        if not item:
            raise SpotifyAPIError(f"Spotify не нашёл плейлист «{query}»")
        return {"id": str(item.get("id", "") or ""), "uri": str(item.get("uri", "") or ""), "name": str(item.get("name", "") or "")}

    @serialized_transport
    def execute(self, action: str, **kwargs: Any) -> dict[str, Any]:
        self._navigation = str(action or "").casefold()
        try:
            return self._execute(action, **kwargs)
        finally:
            self._navigation = ""

    def _execute(self, action: str, **kwargs: Any) -> dict[str, Any]:
        action = str(action or "status").casefold()
        if action == "open":
            opened = bool(webbrowser.open("https://open.spotify.com"))
            return {"provider": "spotify", "verified": opened, "opened": opened, "url": "https://open.spotify.com"}
        if action in {"connect", "authorize"}:
            from jarvis.ui.desktop_events import request_desktop
            queued = request_desktop("settings.open", section="spotify")
            if not queued:
                raise SpotifyAPIError("NEO не удалось открыть настройки подключения Spotify")
            return {"provider": "spotify", "verified": False, "pending": True, "settings_requested": True}
        if action == "queue_status":
            return self.queue_status()
        if action == "status":
            return self.status()
        if action == "background_status":
            return self.status(allow_cached=True, max_age_seconds=4.0)
        if action == "devices":
            return {"verified": True, "devices": self.devices(), "provider": "spotify"}
        if action in {"search_track", "find_track", "find_song"}:
            track = self.search_track(str(kwargs.get("query", "") or ""))
            return {**track, "matches": [track], "provider": "spotify", "verified": True,
                    "search_only": True, "playback_changed": False}
        if action == "play_track":
            track = self.search_track(str(kwargs.get("query", "") or ""))
            device = self._choose_device()
            self._request("PUT", "/me/player/play", params={"device_id": device}, json_body={"uris": [track["uri"]]})
            time.sleep(0.35)
            return {**track, "verified": True, "playing": True, "state": "playing", "provider": "spotify"}
        if action == "playlist":
            playlist = self.search_playlist(str(kwargs.get("playlist") or kwargs.get("query") or ""))
            device = self._choose_device()
            self._request("PUT", "/me/player/play", params={"device_id": device}, json_body={"context_uri": playlist["uri"]})
            return {"verified": True, "playing": True, "state": "playing", "provider": "spotify", "playlist": playlist["name"]}
        if action == "play":
            self._request("PUT", "/me/player/play")
        elif action == "pause":
            self._request("PUT", "/me/player/pause")
        elif action == "stop":
            self._request("PUT", "/me/player/pause")
        elif action == "next":
            self._request("POST", "/me/player/next")
        elif action == "previous":
            self._request("POST", "/me/player/previous")
        elif action == "shuffle_on":
            self._request("PUT", "/me/player/shuffle", params={"state": "true"})
            if self._status_cache: self._status_cache["shuffle"] = True; self._status_cache_at = time.monotonic()
        elif action == "shuffle_off":
            self._request("PUT", "/me/player/shuffle", params={"state": "false"})
            if self._status_cache: self._status_cache["shuffle"] = False; self._status_cache_at = time.monotonic()
        elif action in {"repeat_on", "repeat_track"}:
            self._request("PUT", "/me/player/repeat", params={"state": "track"})
            if self._status_cache: self._status_cache["repeat_mode"] = "track"; self._status_cache_at = time.monotonic()
        elif action == "repeat_playlist":
            self._request("PUT", "/me/player/repeat", params={"state": "context"})
            if self._status_cache: self._status_cache["repeat_mode"] = "playlist"; self._status_cache_at = time.monotonic()
        elif action == "repeat_off":
            self._request("PUT", "/me/player/repeat", params={"state": "off"})
            if self._status_cache: self._status_cache["repeat_mode"] = "off"; self._status_cache_at = time.monotonic()
        elif action == "set_volume":
            raw = kwargs.get("volume", 100)
            value = max(0, min(100, int(float(100 if raw is None else raw))))
            self._request("PUT", "/me/player/volume", params={"volume_percent": value})
            if self._status_cache:
                self._status_cache["volume"] = value
                device = dict(self._status_cache.get("device") or {})
                device["volume_percent"] = value
                self._status_cache["device"] = device
                self._status_cache_at = time.monotonic()
        elif action in {"volume_up", "volume_down"}:
            status = self.status()
            step = int(kwargs.get("step", 10) or 10)
            current_volume = status.get("volume", 50)
            value = max(0, min(100, int(50 if current_volume is None else current_volume) + (step if action == "volume_up" else -step)))
            self._request("PUT", "/me/player/volume", params={"volume_percent": value})
        elif action in {"seek", "seek_to"}:
            raw_position = kwargs.get("position_ms")
            if raw_position is None:
                raw_position = float(kwargs.get("seconds") or 0) * 1000
            position_ms = max(0, int(float(raw_position)))
            self._request("PUT", "/me/player/seek", params={"position_ms": position_ms})
        elif action in {"queue_add", "queue_next"}:
            track = self.search_track(str(kwargs.get("query", "") or ""))
            self._request("POST", "/me/player/queue", params={"uri": track["uri"]})
            return {**track, "verified": True, "queued": True, "provider": "spotify"}
        elif action in {"like", "unlike"}:
            current = self.status()
            track_id = str(current.get("track_id", "") or "")
            if not track_id:
                raise SpotifyAPIError("Нет текущего трека Spotify")
            self._request("PUT" if action == "like" else "DELETE", "/me/tracks", params={"ids": track_id})
        else:
            raise ValueError(f"Неизвестное действие Spotify: {action}")
        time.sleep(0.15)
        data = self.status()
        data["verified"] = True
        return data
