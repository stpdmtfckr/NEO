"""Small, shared, non-starting Ollama readiness snapshot with bounded retry.

Startup diagnostics and UI share a single-flight probe. Missing optional service
is a capability state, never an automatic download or a false ready result.
"""
from __future__ import annotations
from collections import OrderedDict
import threading
import time
from urllib.parse import urlsplit
import requests

_LOCK = threading.Lock()
_CACHE = OrderedDict()


def reset_status(base_url: str | None = None) -> None:
    if not _LOCK.acquire(blocking=False):
        return  # UI must never wait behind an in-flight network probe.
    try:
        if base_url is None:
            _CACHE.clear()
        else:
            _CACHE.pop(str(base_url).rstrip('/'), None)
    finally:
        _LOCK.release()


def readiness(base_url: str, *, force: bool = False, retries: int = 1) -> dict:
    url = str(base_url or '').strip().rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http','https'} or parsed.hostname not in {'localhost','127.0.0.1','::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'','/'}:
        return {'ready':False,'models':[],'status':'invalid_endpoint','message':'Для локальных моделей укажите адрес Ollama на localhost.'}
    with _LOCK:
        now = time.monotonic()
        cached = _CACHE.get(url)
        if not force and cached and now - cached[0] < (10.0 if cached[1]['ready'] else 2.0):
            return dict(cached[1], models=list(cached[1]['models']))
        result = {'ready':False, 'models':[], 'status':'unavailable',
                  'message':'Локальная служба Ollama не отвечает. Запустите Ollama и нажмите «Проверить» в настройках модели. Если Ollama не установлена: https://ollama.com/download. Команды без модели продолжают работать.'}
        with requests.Session() as session:
            session.trust_env = False
            for attempt in range(min(2, max(0, int(retries))) + 1):
                try:
                    with session.get(url + '/api/tags', timeout=(1.0, 2.0), allow_redirects=False) as response:
                        if 300 <= response.status_code < 400:
                            raise ValueError('Local redirect forbidden')
                        response.raise_for_status()
                        data = response.json()
                    if not isinstance(data, dict) or not isinstance(data.get('models'), list):
                        raise ValueError('Invalid model list')
                    names = [str(x['name']) for x in data['models'] if isinstance(x,dict) and x.get('name')]
                    result = {'ready':True,'models':names,'status':'ready','message':f'Локальная служба Ollama доступна; моделей: {len(names)}.'}
                    break
                except (requests.RequestException, OSError, ValueError):
                    if attempt < min(2, max(0,int(retries))):
                        time.sleep(0.2 * (attempt+1))
        _CACHE[url] = (time.monotonic(), result)
        _CACHE.move_to_end(url)
        while len(_CACHE)>8:
            _CACHE.popitem(last=False)
        return dict(result, models=list(result['models']))
