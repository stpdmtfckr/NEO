# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Small secret-store helpers.

On Windows, persistent secrets are protected with DPAPI and are therefore tied
to the current Windows user.  The non-Windows fallback exists only so tests and
portable development environments can round-trip configuration files; it is
explicitly marked as non-secure.
"""

import base64
import threading
from collections import OrderedDict
import ctypes
import os
from ctypes import wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob_from_bytes(data: bytes) -> tuple[_DATA_BLOB, object]:
    if not data:
        buf = ctypes.create_string_buffer(b"\0")
        return _DATA_BLOB(0, ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def protect_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    raw = text.encode("utf-8")
    if os.name != "nt":
        # Development/test fallback. Never presented as encrypted storage.
        return "plain64:" + base64.b64encode(raw).decode("ascii")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, keepalive = _blob_from_bytes(raw)
    out_blob = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "NEO secret",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    del keepalive
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI could not protect secret")
    try:
        protected = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


_UNPROTECTED: "OrderedDict[str, str]" = OrderedDict()
_UNPROTECTED_LOCK = threading.Lock()


def unprotect_secret(value: str) -> str:
    """NEO 173: a DPAPI value is decrypted once per NEO run (clients are created per command)."""
    token = str(value or "").strip()
    if not token.startswith("dpapi:"):
        return _unprotect(token)
    with _UNPROTECTED_LOCK:
        if token in _UNPROTECTED:
            _UNPROTECTED.move_to_end(token)
            return _UNPROTECTED[token]
    plain = _unprotect(token)
    if plain:
        with _UNPROTECTED_LOCK:
            _UNPROTECTED[token] = plain
            while len(_UNPROTECTED) > 32:
                _UNPROTECTED.popitem(last=False)
    return plain


def _unprotect(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if token.startswith("plain64:"):
        try:
            return base64.b64decode(token[8:].encode("ascii")).decode("utf-8")
        except Exception:
            return ""
    if not token.startswith("dpapi:"):
        # Compatibility with a manually supplied historical plaintext value.
        return token if os.name != "nt" else ""
    if os.name != "nt":
        return ""

    try:
        encrypted = base64.b64decode(token[6:].encode("ascii"))
    except Exception:
        return ""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, keepalive = _blob_from_bytes(encrypted)
    out_blob = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    del keepalive
    if not ok:
        return ""
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return raw.decode("utf-8")
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


def yandex_music_token(config) -> str:
    env = str(os.getenv("NEO_JARVIS_YANDEX_MUSIC_TOKEN", "") or "").strip()
    if env:
        return env
    try:
        stored = str(config.get("integrations.yandex_music.token_encrypted", "") or "")
    except Exception:
        stored = ""
    return unprotect_secret(stored)


def discord_bot_token(config, profile: dict | None = None) -> str:
    """Return the official Discord bot token for one configured server profile.

    Per-server DPAPI secrets take precedence.  The environment variable and the
    legacy global encrypted token remain compatibility fallbacks for older
    settings, but a token is never read from a normal plaintext profile field.
    """
    if isinstance(profile, dict):
        stored_profile = str(profile.get("bot_token_encrypted", "") or "").strip()
        if stored_profile:
            return unprotect_secret(stored_profile)
    env = str(os.getenv("NEO_JARVIS_DISCORD_BOT_TOKEN", "") or "").strip()
    if env:
        return env
    try:
        stored = str(config.get("integrations.discord.bot_token_encrypted", "") or "")
    except Exception:
        stored = ""
    return unprotect_secret(stored)

def telegram_bot_token(config) -> str:
    """Return the Telegram Bot API token from DPAPI storage or an env override."""
    env = str(os.getenv("NEO_JARVIS_TELEGRAM_BOT_TOKEN", "") or "").strip()
    if env:
        return env
    try:
        stored = str(config.get("integrations.telegram.bot_token_encrypted", "") or "")
    except Exception:
        stored = ""
    return unprotect_secret(stored)


def telegram_pairing_secret(config) -> str:
    """Return the short-lived one-time Telegram pairing secret."""
    try:
        stored = str(config.get("integrations.telegram.pairing_secret_encrypted", "") or "")
    except Exception:
        stored = ""
    return unprotect_secret(stored)



def telegram_critical_phrase(config) -> str:
    """Return the DPAPI-protected Telegram critical-action phrase."""
    try:
        stored = str(config.get("integrations.telegram.critical_phrase_encrypted", "") or "")
    except Exception:
        stored = ""
    return unprotect_secret(stored)
