"""Recover a Discord Gateway with a new client/HTTP transport per attempt.

Only public discord.py APIs are used. close()+clear()+start() does not rebuild
an owned, closed aiohttp connector in discord.py 2.7.1. Factories also rebind
callbacks to the new client instead of retaining stale Message HTTP state.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import traceback
from typing import Any, Callable

from jarvis.runtime.diagnostics import redact

logger = logging.getLogger("jarvis.integrations.background.discord_bot")
LOGIN_TIMEOUT = 20.0
CLOSE_TIMEOUT = 5.0


def safe_error(exc: BaseException, token: str) -> str:
    text = str(exc) or type(exc).__name__
    if token:
        text = text.replace(token, "[REDACTED]")
    return redact(text)[:1200]


def safe_traceback(exc: BaseException, token: str) -> str:
    # No frame locals. Replace the actual credential before generic redaction.
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if token:
        text = text.replace(token, "[REDACTED]")
    return redact(text)[-10000:]


def _fatal(exc: BaseException) -> bool:
    return (type(exc).__name__ in {"LoginFailure", "PrivilegedIntentsRequired"}
            or getattr(exc, "code", 0) in {4004, 4014})


def _admission(owner: Any) -> Any:
    getter = getattr(owner, "admission", None)
    if callable(getter):
        return getter()
    return 0 if getattr(owner, "_enabled", lambda: True)() else None


async def run_gateway(owner: Any, factory: Callable[[], Any], token: str) -> None:
    """Run until explicitly closed or credentials/teardown require attention.

    The login has a finite deadline; the connected session itself is not subject
    to that deadline. A failed teardown stops recovery rather than running two
    clients concurrently. Cancellation is never converted to a network failure.
    """
    owner._network_changed = asyncio.Event()
    delay = 2.0
    previous_error = ""
    last_error_log = 0.0
    suppressed = 0
    while not owner._closing.is_set():
        if not getattr(owner, "_enabled", lambda: True)():
            # Disabled in Settings: no new login or reconnect attempts.
            owner._gateway_phase = "disabled"
            owner._error = "Discord отключён в настройках"
            owner._failed.set()
            owner._ready.set()
            return
        client = None
        terminal = False
        disabled = False
        owner._gateway_phase = "constructing"
        owner._gateway_attempts = getattr(owner, "_gateway_attempts", 0) + 1
        owner._gateway_ready_at = 0.0
        owner._ready.clear()
        try:
            client = factory()  # Called *inside* the running asyncio loop.
            with owner._lock:
                owner._client = client
            owner._persistent_music_view_registered = False
            for name in ("_music_dashboard_messages", "_timer_dashboard_messages"):
                getattr(owner, name, {}).clear()
            owner._gateway_phase = "login"
            admitted = _admission(owner)
            await asyncio.wait_for(client.login(token), timeout=LOGIN_TIMEOUT)
            if owner._closing.is_set() or _admission(owner) != admitted or admitted is None:
                # Switched off (or closed) while logging in: no Gateway connect.
                disabled = True
            else:
                owner._gateway_phase = "gateway"
                await client.connect(reconnect=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            owner._error = safe_error(exc, token)
            terminal = _fatal(exc)
            fingerprint = f"{owner._gateway_phase}:{type(exc).__name__}:{owner._error}"
            now = time.monotonic()
            should_log = terminal or fingerprint != previous_error or now - last_error_log >= 120.0
            if should_log:
                logger.log(logging.ERROR if terminal else logging.WARNING,
                    "Discord Gateway %s: phase=%s attempt=%s type=%s detail=%s; suppressed=%s",
                    "authorization/configuration rejected" if terminal else "unavailable",
                    owner._gateway_phase, owner._gateway_attempts, type(exc).__name__, owner._error, suppressed)
                logger.log(logging.ERROR if terminal else logging.WARNING,
                           "Discord failure traceback (credentials redacted):\n%s", safe_traceback(exc, token))
                last_error_log, suppressed = now, 0
            else:
                suppressed += 1
            previous_error = fingerprint
        finally:
            # A clean result requires completion, not simply a close() request.
            owner._ready.clear()
            if client is not None:
                try:
                    await asyncio.wait_for(client.close(), timeout=CLOSE_TIMEOUT)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    terminal = True
                    owner._error = "Discord client teardown failed: " + safe_error(exc, token)
                    logger.error("%s; no overlapping replacement client will be created", owner._error)
                with owner._lock:
                    if owner._client is client:
                        owner._client = None
        if owner._closing.is_set():
            owner._gateway_phase = "stopped"
            return
        if disabled or _admission(owner) is None:
            owner._gateway_phase = "disabled"
            owner._error = "Discord отключён в настройках"
            owner._failed.set()
            owner._ready.set()
            return
        if terminal:
            owner._gateway_phase = "failed"
            owner._failed.set()
            owner._ready.set()  # Wake ensure_ready(), which then checks _failed.
            return
        ready_at = float(getattr(owner, "_gateway_ready_at", 0.0) or 0.0)
        if ready_at and time.monotonic() - ready_at >= 60.0:
            delay = 2.0
        owner._gateway_phase = "retry_wait"
        retry = min(30.0, delay + random.uniform(0, delay * 0.15))
        owner._gateway_retry_delay = retry
        logger.debug("Discord reconnect in %.2fs using a new client", retry)
        try:
            await asyncio.wait_for(owner._network_changed.wait(), timeout=retry)
        except TimeoutError:
            pass
        owner._network_changed.clear()
        delay = min(30.0, delay * 1.7)
