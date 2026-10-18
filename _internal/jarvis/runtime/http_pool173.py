"""NEO 173: faster web services — shared connections, warm-up at the wake word, short caches, timings.

* ``session(service)`` — one ``requests.Session`` per service for the whole NEO process.
  Before, Gmail and Calendar created a new session for every command, so every command
  paid a new TCP + TLS handshake (two hosts for Gmail).
* ``prewarm(config)`` — when the wake word is heard, the TLS connections to the services
  the user has *enabled* are opened in the background while NEO still listens to the
  command. Only a connection is opened: no HTTP request, no data is sent. At most once a
  minute per host.
* ``ResponseCache`` — GET answers of a service kept for a few seconds (unread mail count,
  calendar events); any change through NEO (send, mark read, create/delete event) clears it.
* ``timed`` / ``timings`` — how long each service took (for the developer report).
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import json
import logging
import threading
import time
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SESSIONS: dict[str, Any] = {}
_TIMINGS: deque[dict[str, Any]] = deque(maxlen=400)
_WARMED: dict[str, float] = {}
WARM_EVERY = 60.0


def session(service: str, *, trust_env: bool | None = None):
    """The shared session of a service (created on first use)."""
    import requests
    with _LOCK:
        existing = _SESSIONS.get(service)
        if existing is not None:
            return existing
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        s.mount('https://', adapter)
        s.mount('http://', adapter)
        if trust_env is not None:
            s.trust_env = trust_env
        _SESSIONS[service] = s
        return s


def reset(service: str):
    """After a network change: a fresh session (old connections may point at the old network)."""
    with _LOCK:
        old = _SESSIONS.pop(service, None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    return session(service)


def close_all() -> None:
    with _LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for s in sessions:
        try:
            s.close()
        except Exception:
            pass


# ---- timings ----------------------------------------------------------------------------------

@contextmanager
def timed(service: str, stage: str = 'request') -> Iterator[dict[str, Any]]:
    row: dict[str, Any] = {'service': service, 'stage': stage, 'ok': True}
    started = time.perf_counter()
    try:
        yield row
    except Exception as exc:
        row['ok'] = False
        row['error'] = type(exc).__name__
        raise
    finally:
        row['ms'] = round((time.perf_counter() - started) * 1000, 1)
        row['time'] = time.time()
        with _LOCK:
            _TIMINGS.append(row)


def record(service: str, stage: str, ms: float, ok: bool = True, **extra: Any) -> None:
    with _LOCK:
        _TIMINGS.append({'service': service, 'stage': stage, 'ms': round(float(ms), 1), 'ok': ok, 'time': time.time(), **extra})


def timings(service: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        rows = list(_TIMINGS)
    return [r for r in rows if service is None or r['service'] == service]


def summary() -> dict[str, dict[str, float]]:
    """Per service: count, median and slowest time in ms (successful calls)."""
    out: dict[str, dict[str, float]] = {}
    groups: dict[str, list[float]] = {}
    for row in timings():
        if row.get('ok'):
            groups.setdefault(f"{row['service']}.{row['stage']}", []).append(float(row['ms']))
    for key, values in sorted(groups.items()):
        values.sort()
        out[key] = {'count': len(values), 'median_ms': values[len(values) // 2], 'max_ms': values[-1]}
    return out


# ---- warm-up ------------------------------------------------------------------------------------

# service → (config switch that says the user uses it, hosts)
SERVICES: dict[str, tuple[Callable[[Any], bool], tuple[str, ...]]] = {
    'searchapi': (lambda c: bool(c.get('gaming.web_knowledge.enabled', True))
                  and bool(c.get('gaming.web_knowledge.searchapi_api_key_encrypted', '')), ('https://www.searchapi.io',)),
    'gmail': (lambda c: bool(c.get('integrations.gmail.enabled', False)) and bool(c.get('integrations.gmail.refresh_token_encrypted', '')),
              ('https://gmail.googleapis.com', 'https://oauth2.googleapis.com')),
    'calendar': (lambda c: bool(c.get('integrations.google_calendar.enabled', False)), ('https://www.googleapis.com',)),
    'spotify': (lambda c: bool(c.get('integrations.spotify.enabled', False)), ('https://api.spotify.com',)),
    'yandex_music': (lambda c: bool(c.get('integrations.yandex_music.enabled', False)), ('https://api.music.yandex.net',)),
    'weather': (lambda c: bool(c.get('integrations.openweather.enabled', False)), ('https://api.openweathermap.org',)),
}


def _open_connection(s: Any, url: str) -> bool:
    """Open (and pool) a TLS connection to ``url`` without sending any HTTP request."""
    adapter = s.get_adapter(url)
    pool = adapter.poolmanager.connection_from_url(url)
    conn = pool._get_conn(timeout=0.5)
    try:
        if getattr(conn, 'sock', None) is None:
            conn.connect()
        return True
    finally:
        pool._put_conn(conn)


def hosts_to_warm(config: Any, now: float | None = None) -> list[tuple[str, str]]:
    now = time.monotonic() if now is None else now
    out = []
    for service, (enabled, hosts) in SERVICES.items():
        try:
            if not enabled(config):
                continue
        except Exception:
            continue
        for host in hosts:
            with _LOCK:
                if now - _WARMED.get(host, -1e9) < WARM_EVERY:
                    continue
                _WARMED[host] = now
            out.append((service, host))
    return out


def prewarm(config: Any, *, opener: Callable[[Any, str], bool] = _open_connection, background: bool = True) -> list[tuple[str, str]]:
    """Warm the connections of the services the user has turned on; returns what was scheduled."""
    if not bool(config.get('performance.prewarm_connections', True)):
        return []
    targets = hosts_to_warm(config)
    if not targets:
        return []

    def work() -> None:
        for service, host in targets:
            started = time.perf_counter()
            try:
                opener(session(service), host)
                record(service, 'warm', (time.perf_counter() - started) * 1000)
            except Exception as exc:
                record(service, 'warm', (time.perf_counter() - started) * 1000, ok=False, error=type(exc).__name__)
    if background:
        threading.Thread(target=work, name='NeoJarvis-Prewarm', daemon=True).start()
    else:
        work()
    return targets


# ---- short response cache ---------------------------------------------------------------------

class ResponseCache:
    """GET answers for ``ttl`` seconds; ``clear()`` after any change made through NEO."""

    def __init__(self, ttl: float, limit: int = 64) -> None:
        self.ttl, self.limit = float(ttl), int(limit)
        self._rows: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(method: str, path: str, params: Any = None, extra: str = '') -> str:
        return json.dumps([method.upper(), path, params or {}, extra], sort_keys=True, ensure_ascii=False, default=str)

    def get(self, key: str) -> Any:
        with self._lock:
            row = self._rows.get(key)
            if row is None or time.monotonic() - row[0] > self.ttl:
                self._rows.pop(key, None)
                return None
            return row[1]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._rows[key] = (time.monotonic(), value)
            while len(self._rows) > self.limit:
                self._rows.pop(next(iter(self._rows)))

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


__all__ = ['ResponseCache', 'SERVICES', 'close_all', 'reset', 'hosts_to_warm', 'prewarm', 'record', 'session', 'summary', 'timed', 'timings']
