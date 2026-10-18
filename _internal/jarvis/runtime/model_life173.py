"""NEO 173: the NEO model is in memory while you talk to NEO, and not much longer.

* Every answer keeps the model loaded for ``llm_agent.keep_alive`` (now 5 minutes, was 15):
  a conversation keeps it loaded, silence unloads it (Ollama unloads by itself).
* When the wake word is heard, the model is loaded in the background *while you are still
  speaking* — after a long pause the first answer does not wait for the load.
* When a game starts, NEO's models are unloaded at once (setting), so the game gets the memory.
* On exit NEO unloads its models (the NEO model was missing from that list before).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_LAST_PRELOAD = [0.0]
_PRELOAD_INFLIGHT = [False]
_RELEASE_INFLIGHT = [False]
PRELOAD_EVERY = 20.0


def _base_url(config: Any) -> str:
    return str(config.get('ollama.base_url', 'http://127.0.0.1:11434') or 'http://127.0.0.1:11434').rstrip('/')


def loaded_models(config: Any, session: Any, *, base_url: str | None = None) -> set[str]:
    """Models Ollama has in memory right now (``/api/ps``)."""
    response = session.get(f'{base_url or _base_url(config)}/api/ps', timeout=(0.5, 1.5))
    response.raise_for_status()
    return {str(m.get('name') or m.get('model') or '') for m in (response.json() or {}).get('models', [])}


def preload(ollama: Any, *, background: bool = True, now: float | None = None) -> bool:
    """Load the NEO model if it is not in memory (at most every 20 s). Returns whether a load was scheduled."""
    config = getattr(ollama, 'config', None)
    if config is None or not bool(config.get('llm_agent.enabled', True)) or not bool(config.get('performance.preload_on_wake', True)):
        return False
    now = time.monotonic() if now is None else now
    with _LOCK:
        if _PRELOAD_INFLIGHT[0] or now - _LAST_PRELOAD[0] < PRELOAD_EVERY:
            return False
        _LAST_PRELOAD[0] = now
        _PRELOAD_INFLIGHT[0] = True

    request = ollama._capture_request("agent") if hasattr(ollama, "_capture_request") else None
    base_url = request.base_url if request is not None else _base_url(config)

    def work() -> None:
        started = time.perf_counter()
        try:
            if request is not None:
                ollama._check_request(request)
            model = ollama.resolve_agent_model()
            session = ollama._session
            if request is not None:
                ollama._check_request(request)
            if model in loaded_models(config, session, base_url=base_url):
                return
            if request is not None:
                ollama._check_request(request)
            # A generate request without a prompt only loads the model (Ollama API).
            # Match the next planner/chat request's context and runner options;
            # otherwise Ollama can reload immediately to change its context size.
            options = ollama._runner_resource_options() if hasattr(ollama, '_runner_resource_options') else {}
            options['num_ctx'] = int(config.get('llm_agent.num_ctx', 3072) or 3072)
            response = session.post(f'{base_url}/api/generate',
                         json={'model': model, 'keep_alive': config.get('llm_agent.keep_alive', '5m'), 'options': options}, timeout=(1.0, 60.0))
            response.raise_for_status()
            response.close()
            from jarvis.runtime import http_pool173
            http_pool173.record('ollama', 'preload', (time.perf_counter() - started) * 1000)
            logger.info('NEO model %s loaded at the wake word in %.0f ms', model, (time.perf_counter() - started) * 1000)
        except Exception as exc:
            logger.debug('Model preload skipped: %s', exc)
        finally:
            with _LOCK:
                _PRELOAD_INFLIGHT[0] = False
    if background:
        try:
            threading.Thread(target=work, name='NeoJarvis-ModelPreload', daemon=True).start()
        except RuntimeError:
            with _LOCK:
                _PRELOAD_INFLIGHT[0] = False
            return False
    else:
        work()
    return True


def release(ollama: Any, *, reason: str = '') -> int:
    """Unload NEO's models now (keep_alive 0): the NEO model, the chat model and the screen model."""
    config = getattr(ollama, 'config', None)
    if config is None:
        return 0
    def selected_names() -> tuple[str, ...]:
        names: list[str] = []
        try:
            names.extend(ollama.agent_model_candidates()[:3])
        except Exception:
            pass
        for key in ('local_chat.model', 'ollama.chat_model', 'ollama.vision_model', 'translation.model'):
            value = str(config.get(key, '') or '')
            if value:
                names.append(value)
        return tuple(dict.fromkeys(names))

    # Capture a coherent target before probing. Settings can change between
    # individual getters; never combine one endpoint with another selection.
    # Once captured, verification/unload deliberately stays on this old target.
    base_url = _base_url(config)
    names = selected_names()
    if base_url != _base_url(config) or names != selected_names() or base_url != _base_url(config):
        return 0
    released = 0
    session = getattr(ollama, '_session', None)
    if session is None:
        return 0
    try:
        loaded = loaded_models(config, session, base_url=base_url)
    except Exception:
        return 0  # No verified loaded models: do not issue guessed unloads.
    for name in dict.fromkeys(names):
        matches = [m for m in loaded if m == name or (":" not in name and m == name + ":latest")]
        for model in matches:
            try:
                response = session.post(f'{base_url}/api/generate', json={'model': model, 'keep_alive': 0}, timeout=3)
                response.raise_for_status()
                response.close()
                released += 1
            except Exception:
                continue
    if released:
        logger.info('Unloaded %d NEO model(s)%s', released, f' ({reason})' if reason else '')
    return released


_REGISTERED: list[Any] = [None]


def register(ollama: Any) -> None:
    """The main window registers NEO's Ollama client so the game tracker can free memory."""
    _REGISTERED[0] = ollama


def release_for_game(ollama: Any = None) -> bool:
    ollama = ollama or _REGISTERED[0]
    config = getattr(ollama, 'config', None)
    if config is None or not bool(config.get('performance.unload_models_in_games', True)):
        return False
    with _LOCK:
        if _RELEASE_INFLIGHT[0]:
            return False
        _RELEASE_INFLIGHT[0] = True
    def work() -> None:
        try:
            release(ollama, reason='game started')
        finally:
            with _LOCK:
                _RELEASE_INFLIGHT[0] = False
    try:
        threading.Thread(target=work, name='NeoJarvis-ModelRelease', daemon=True).start()
        return True
    except RuntimeError:
        with _LOCK:
            _RELEASE_INFLIGHT[0] = False
        return False


__all__ = ['loaded_models', 'preload', 'register', 'release', 'release_for_game']
