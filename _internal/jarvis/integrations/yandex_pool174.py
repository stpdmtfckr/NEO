"""NEO 174: Yandex Music requests over one kept-alive connection.

The yandex-music library (3.x) sends every API call through ``requests.request``,
which opens a new TCP + TLS connection each time. Starting «Моя волна» makes
several calls in a row (session start, tracks, download info, the file link), and
each paid a fresh handshake. ``PooledRequest`` sends the same requests, with the
same headers, timeouts and error handling, through NEO's shared connection pool
for Yandex Music (the one warmed at the wake word). Cookies are not kept between
requests, exactly like before.
"""
from __future__ import annotations

from http.cookiejar import DefaultCookiePolicy
from typing import Any

_CLASS: list[Any] = []


def _session():
    from jarvis.runtime import http_pool173
    session = http_pool173.session('yandex_music')
    if not getattr(session, '_neo_no_cookies', False):
        session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))    # like a fresh request each time
        session._neo_no_cookies = True
    return session


def request_class() -> Any:
    """``yandex_music.utils.request.Request`` that reuses NEO's connection (created once)."""
    if _CLASS:
        return _CLASS[0]
    import requests
    from yandex_music.exceptions import NetworkError, TimedOutError
    from yandex_music.utils.request import Request
    from jarvis.runtime import http_pool173

    class PooledRequest(Request):
        def _request_wrapper(self, *args: Any, **kwargs: Any) -> bytes:
            kwargs = self._prepare_kwargs(kwargs)
            method = str(args[0] if args else kwargs.get('method', 'GET')).upper()
            try:
                with http_pool173.timed('yandex_music', method):
                    response = _session().request(*args, **kwargs)
            except requests.Timeout as exc:
                raise TimedOutError from exc
            except requests.RequestException as exc:
                raise NetworkError(exc) from exc
            if 200 <= response.status_code <= 299:
                return response.content
            self._handle_error_response(response.status_code, response.content)
            return None

    _CLASS.append(PooledRequest)
    return PooledRequest


def make_request(config: Any, **kwargs: Any) -> Any:
    """A request object for the Yandex Music client: pooled unless turned off in settings."""
    if config is None or bool(config.get('performance.pooled_yandex', True)):
        try:
            return request_class()(**kwargs)
        except Exception:
            pass
    from yandex_music.utils.request import Request
    return Request(**kwargs)
