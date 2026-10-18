# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Unified Gmail + Mail.ru integration for NEO.

Gmail uses Google's OAuth 2.0 installed-app flow (PKCE) and the Gmail REST API.
Mail.ru uses its documented IMAP/SMTP SSL endpoints with an application password.
No mailbox password or OAuth access token is ever logged by this module.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
import base64
import hashlib
import html
import imaplib
import json
import logging
import re
import secrets
import smtplib
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

import requests

from jarvis.secrets import protect_secret, unprotect_secret

logger = logging.getLogger(__name__)

GMAIL_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.modify",)
MAILRU_IMAP_HOST = "imap.mail.ru"
MAILRU_IMAP_PORT = 993
MAILRU_SMTP_HOST = "smtp.mail.ru"
MAILRU_SMTP_PORT = 465
_GMAIL_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _reads_cache():
    from jarvis.runtime.http_pool173 import ResponseCache
    return ResponseCache(ttl=20.0, limit=64)


_GMAIL_READS = _reads_cache()
_GMAIL_TOKEN_CACHE_LOCK = threading.Lock()


@dataclass(slots=True)
class MailMessage:
    provider: str
    message_id: str
    thread_id: str = ""
    sender: str = ""
    sender_email: str = ""
    subject: str = ""
    preview: str = ""
    body: str = ""
    date: str = ""
    unread: bool = False
    rfc_message_id: str = ""
    folder: str = ""
    received_ts: float = 0.0

    def compact(self) -> dict[str, Any]:
        data = asdict(self)
        if len(data.get("body", "")) > 5000:
            data["body"] = data["body"][:5000] + "…"
        return data


@dataclass(slots=True)
class MailFolderStatus:
    name: str
    total: int = 0
    unread: int = 0
    recent24h: int = 0
    today: int = 0
    unread_today: int = 0

    def compact(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": int(self.total),
            "unread": int(self.unread),
            "recent24h": int(self.recent24h),
            "today": int(self.today),
            "unread_today": int(self.unread_today),
        }


@dataclass(slots=True)
class MailStatus:
    provider: str
    total: int = 0
    unread: int = 0
    recent24h: int = 0
    today: int = 0
    unread_today: int = 0
    account: str = ""
    latest: list[MailMessage] | None = None
    unread_latest: list[MailMessage] | None = None
    folders: list[MailFolderStatus] | None = None
    unavailable_folders: list[str] | None = None

    def compact(self) -> dict[str, Any]:
        result = {
            "provider": self.provider,
            "total": int(self.total),
            "unread": int(self.unread),
            "recent24h": int(self.recent24h),
            "today": int(self.today),
            "unread_today": int(self.unread_today),
            "account": self.account,
            "latest": [m.compact() for m in (self.latest or [])],
            "unread_latest": [m.compact() for m in (self.unread_latest or [])],
            "folders": [f.compact() for f in (self.folders or [])],
        }
        if self.unavailable_folders:
            result['partial'] = True
            result['unavailable_folders'] = list(self.unavailable_folders)
        return result


def _decode_header(value: str | None) -> str:
    raw = str(value or "")
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def _clean_text(value: str, limit: int = 4000) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _message_body(msg: Message, limit: int = 8000) -> str:
    candidates: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            if ctype not in {"text/plain", "text/html"}:
                continue
            try:
                value = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                value = payload.decode(charset, errors="replace")
            if value:
                cleaned = _clean_text(str(value), limit=limit)
                if cleaned:
                    if ctype == "text/plain":
                        return cleaned
                    candidates.append(cleaned)
    else:
        try:
            value = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            value = payload.decode(charset, errors="replace")
        if value:
            return _clean_text(str(value), limit=limit)
    return candidates[0][:limit] if candidates else ""


def _preview(value: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", _clean_text(value, limit=max(limit * 3, 600))).strip()
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _normalize_provider(provider: str) -> str:
    value = str(provider or "auto").strip().casefold()
    if value in {"mail", "mailru", "mail.ru", "мейл", "мейл.ру"}:
        return "mailru"
    if value in {"gmail", "google", "googlemail"}:
        return "gmail"
    return value or "auto"


def _split_folder_names(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(x or "") for x in value]
    else:
        raw_items = re.split(r"[;,\n]+", str(value or ""))
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        name = re.sub(r"\s+", " ", raw).strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


# HIST-032: one logical inbox, whatever it is called in the folder list.
_INBOX_ALIASES = frozenset({"inbox", "входящие", "posteingang", "boîte de réception", "boite de reception",
                            "bandeja de entrada", "posta in arrivo", "caixa de entrada"})


def _mail_folder_key(name: str) -> str:
    key = " ".join(str(name or "").split()).casefold()
    return "inbox" if key in _INBOX_ALIASES else key


def selected_mail_folders(config, provider: str) -> list[str]:
    p = _normalize_provider(provider)
    cfg = f"integrations.{p}"
    out: list[str] = []
    seen: set[str] = set()
    if bool(config.get(f"{cfg}.include_inbox", True)):
        out.append("Входящие")
        seen.add("inbox")
    for name in _split_folder_names(config.get(f"{cfg}.additional_folders", [])):
        key = _mail_folder_key(name)
        if key not in seen:
            seen.add(key)
            out.append("Входящие" if key == "inbox" else name)
    return out


def _local_midnight_epoch() -> int:
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _message_received_ts(value: str) -> float:
    try:
        dt = parsedate_to_datetime(str(value or ""))
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except Exception:
        return 0.0


def _sort_messages(rows: Iterable[MailMessage], limit: int = 50) -> list[MailMessage]:
    dedup: dict[tuple[str, str], MailMessage] = {}
    for item in rows:
        key = (str(item.provider), str(item.message_id))
        previous = dedup.get(key)
        if previous is None or float(item.received_ts or 0.0) > float(previous.received_ts or 0.0):
            dedup[key] = item
    ordered = sorted(dedup.values(), key=lambda m: float(m.received_ts or 0.0), reverse=True)
    return ordered[:max(0, int(limit))]


class GmailClient:
    def __init__(self, config) -> None:
        self.config = config
        self.client_id = unprotect_secret(str(config.get("integrations.gmail.client_id_encrypted", "") or ""))
        self.client_secret = unprotect_secret(str(config.get("integrations.gmail.client_secret_encrypted", "") or ""))
        self.refresh_token = unprotect_secret(str(config.get("integrations.gmail.refresh_token_encrypted", "") or ""))
        self.access_token = unprotect_secret(str(config.get("integrations.gmail.access_token_encrypted", "") or ""))
        self.expires_at = float(config.get("integrations.gmail.token_expires_at", 0.0) or 0.0)
        cache_key = self.client_id + "|" + self.refresh_token[-24:]
        if cache_key.strip("|"):
            with _GMAIL_TOKEN_CACHE_LOCK:
                cached = _GMAIL_TOKEN_CACHE.get(cache_key)
            if cached and cached[1] > self.expires_at:
                self.access_token, self.expires_at = cached
        self._cache_key = cache_key
        self.account_email = str(config.get("integrations.gmail.account_email", "") or "")
        self.timeout = max(4.0, min(30.0, float(config.get("integrations.gmail.timeout_seconds", 10.0) or 10.0)))
        # NEO 173: one Gmail session for the whole NEO process (a client is created per command;
        # its own session made every command pay a new TLS handshake to two Google hosts).
        from jarvis.runtime import http_pool173
        self._session = http_pool173.session("gmail")

    def configured(self) -> bool:
        return bool(self.client_id and self.refresh_token)

    def _save_tokens(self, payload: dict[str, Any]) -> None:
        # Token refresh can happen on a worker thread. ConfigManager writes are
        # intentionally avoided here; live access token is cached in this client.
        # Settings authorization persists refresh/access tokens explicitly.
        token = str(payload.get("access_token", "") or "")
        if token:
            self.access_token = token
        refresh = str(payload.get("refresh_token", "") or "")
        if refresh:
            self.refresh_token = refresh
        expires = max(60, int(payload.get("expires_in", 3600) or 3600))
        self.expires_at = time.time() + expires - 30
        if self._cache_key.strip("|") and self.access_token:
            with _GMAIL_TOKEN_CACHE_LOCK:
                _GMAIL_TOKEN_CACHE[self._cache_key] = (self.access_token, self.expires_at)

    def _refresh(self) -> None:
        if not self.client_id or not self.refresh_token:
            raise RuntimeError("Gmail не авторизован. Откройте Настройки → API-сервисы → Gmail и нажмите «Авторизовать Gmail».")
        data = {
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        response = self._session.post(GMAIL_TOKEN_URL, data=data, timeout=self.timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"Gmail OAuth refresh: HTTP {response.status_code}")
        self._save_tokens(response.json())

    def token(self) -> str:
        if not self.access_token or self.expires_at <= time.time() + 30:
            self._refresh()
        return self.access_token

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        # NEO 173: identical reads within a few seconds (a count and then the list) are answered
        # from memory; any change made through NEO clears that memory.
        from jarvis.runtime import http_pool173
        # Only NEO's own shared session is cached: a status check or an injected session always
        # talks to the server.
        cache = self._session is http_pool173.session("gmail")
        if method.upper() != "GET":
            _GMAIL_READS.clear()
        elif cache:
            key = http_pool173.ResponseCache.key(method, path, kwargs.get("params"), self._cache_key)
            cached = _GMAIL_READS.get(key)
            if cached is not None:
                return dict(cached)
        with http_pool173.timed("gmail", method.upper()):
            result = self._request_uncached(method, path, **kwargs)
        if method.upper() == "GET" and cache:
            _GMAIL_READS.put(key, dict(result))
        return result

    def _request_uncached(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.token()}"
        response = self._session.request(method, GMAIL_API + path, headers=headers, timeout=self.timeout, **kwargs)
        if response.status_code == 401 and self.refresh_token:
            self._refresh()
            headers["Authorization"] = f"Bearer {self.access_token}"
            response = self._session.request(method, GMAIL_API + path, headers=headers, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("error", {}).get("message", "") or "")
            except Exception:
                pass
            low = detail.casefold()
            if response.status_code == 403 and ("gmail api has not been used" in low or "gmail api" in low and "disabled" in low):
                raise RuntimeError(
                    "Gmail API отключён в Google Cloud для этого OAuth-проекта. "
                    "Включите Gmail API в Google Cloud Console → APIs & Services → Library, "
                    "подождите несколько минут и повторите проверку Gmail."
                )
            raise RuntimeError(f"Gmail API HTTP {response.status_code}" + (f": {detail}" if detail else ""))
        if not response.content:
            return {}
        return dict(response.json() or {})

    def profile(self) -> dict[str, Any]:
        return self._request("GET", "/users/me/profile")

    def _query_ids(self, query: str, cap: int = 10000) -> list[str]:
        out: list[str] = []
        token = ""
        while len(out) < cap:
            params: dict[str, Any] = {"q": str(query or ""), "maxResults": 500}
            if token:
                params["pageToken"] = token
            data = self._request("GET", "/users/me/messages", params=params)
            for row in data.get("messages", []) or []:
                mid = str((row or {}).get("id", "") or "")
                if mid:
                    out.append(mid)
                    if len(out) >= cap:
                        break
            token = str(data.get("nextPageToken", "") or "")
            if not token:
                break
        return out

    def _count_query(self, query: str, cap: int = 10000) -> int:
        return len(self._query_ids(query, cap=cap))

    @staticmethod
    def _headers(payload: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in ((payload or {}).get("headers", []) or []):
            name = str((item or {}).get("name", "") or "").casefold()
            if name:
                out[name] = str((item or {}).get("value", "") or "")
        return out

    @staticmethod
    def _decode_part_body(payload: dict[str, Any]) -> str:
        def walk(part: dict[str, Any]) -> Iterable[tuple[str, str]]:
            mime = str(part.get("mimeType", "") or "")
            body = part.get("body", {}) or {}
            data = str(body.get("data", "") or "")
            if data and mime in {"text/plain", "text/html"}:
                try:
                    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                    yield mime, raw.decode("utf-8", errors="replace")
                except Exception:
                    pass
            for child in part.get("parts", []) or []:
                yield from walk(child or {})
        html_candidate = ""
        for mime, value in walk(payload or {}):
            cleaned = _clean_text(value, 8000)
            if mime == "text/plain" and cleaned:
                return cleaned
            if cleaned and not html_candidate:
                html_candidate = cleaned
        return html_candidate

    @staticmethod
    def _folder_query(folder: str) -> str:
        name = re.sub(r"\s+", " ", str(folder or "")).strip()
        low = name.casefold().replace("ё", "е")
        aliases = {
            "входящие": "in:inbox", "inbox": "in:inbox",
            "отправленные": "in:sent", "sent": "in:sent", "sent mail": "in:sent",
            "черновики": "in:drafts", "drafts": "in:drafts",
            "спам": "in:spam", "spam": "in:spam",
            "корзина": "in:trash", "trash": "in:trash",
            "важные": "is:important", "важное": "is:important", "important": "is:important",
            "помеченные": "is:starred", "избранное": "is:starred", "starred": "is:starred",
            "рассылки": "category:promotions", "промоакции": "category:promotions", "promotions": "category:promotions",
            "соцсети": "category:social", "социальные сети": "category:social", "social": "category:social",
            "обновления": "category:updates", "updates": "category:updates",
            "форумы": "category:forums", "forums": "category:forums",
            "основные": "category:primary", "primary": "category:primary",
            "отправленные себе": "from:me to:me", "письма себе": "from:me to:me", "self": "from:me to:me",
            # 186: the built-in examples shown in the Settings placeholder of every
            # UI language mean the same Gmail categories as the Russian ones.
            "newsletters": "category:promotions", "newsletter": "category:promotions",
            "boletines": "category:promotions",
            "sent to self": "from:me to:me", "an mich selbst gesendet": "from:me to:me",
            "envoyés à soi-même": "from:me to:me", "enviados a mí mismo": "from:me to:me",
            "inviati a me stesso": "from:me to:me", "enviados para mim": "from:me to:me",
        }
        if low in aliases:
            return aliases[low]
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'label:"{escaped}"'

    def selected_folders(self) -> list[str]:
        return selected_mail_folders(self.config, "gmail")

    def _scope_query(self, folder: str | None = None) -> str:
        if folder:
            return self._folder_query(folder)
        folders = self.selected_folders()
        if not folders:
            return 'label:"__neo_no_selected_mail_folder__"'
        queries = [self._folder_query(name) for name in folders]
        if len(queries) == 1:
            return queries[0]
        return " OR ".join(f"({q})" for q in queries)

    @staticmethod
    def _and_query(base: str, extra: str) -> str:
        base = str(base or "").strip()
        extra = str(extra or "").strip()
        if not base:
            return extra
        if not extra:
            return base
        return f"({base}) ({extra})"

    def list_folders(self) -> list[str]:
        data = self._request("GET", "/users/me/labels")
        rows: list[str] = ["Входящие", "Отправленные", "Рассылки", "Отправленные себе"]
        skip_ids = {"UNREAD", "CHAT"}
        for item in data.get("labels", []) or []:
            lid = str((item or {}).get("id", "") or "")
            name = str((item or {}).get("name", "") or "").strip()
            if not name or lid in skip_ids:
                continue
            if name.casefold() not in {x.casefold() for x in rows}:
                rows.append(name)
        return rows

    def get_message(self, message_id: str, full: bool = False, *, folder: str = "") -> MailMessage:
        fmt = "full" if full else "metadata"
        params = {"format": fmt}
        if not full:
            params["metadataHeaders"] = ["From", "Subject", "Date", "Message-ID", "Reply-To", "References", "In-Reply-To"]
        data = self._request("GET", f"/users/me/messages/{message_id}", params=params)
        payload = data.get("payload", {}) or {}
        headers = self._headers(payload)
        sender_raw = _decode_header(headers.get("from", ""))
        sender_name, sender_email = parseaddr(sender_raw)
        body = self._decode_part_body(payload) if full else ""
        snippet = _clean_text(str(data.get("snippet", "") or ""), 500)
        labels = set(str(x) for x in (data.get("labelIds", []) or []))
        received_ts = float(data.get("internalDate", 0) or 0) / 1000.0
        return MailMessage(
            provider="gmail",
            message_id=str(data.get("id", message_id) or message_id),
            thread_id=str(data.get("threadId", "") or ""),
            sender=_decode_header(sender_name) or sender_email or sender_raw,
            sender_email=sender_email,
            subject=_decode_header(headers.get("subject", "")),
            preview=_preview(body or snippet),
            body=body,
            date=str(headers.get("date", "") or ""),
            unread="UNREAD" in labels,
            rfc_message_id=str(headers.get("message-id", "") or ""),
            folder=str(folder or ""),
            received_ts=received_ts or _message_received_ts(str(headers.get("date", "") or "")),
        )

    def _list_one_folder(self, folder: str, *, query: str = "", limit: int = 10, full: bool = False) -> list[MailMessage]:
        q = self._and_query(self._scope_query(folder), query)
        data = self._request("GET", "/users/me/messages", params={"q": q, "maxResults": max(1, min(50, int(limit)))})
        out: list[MailMessage] = []
        for item in data.get("messages", []) or []:
            mid = str((item or {}).get("id", "") or "")
            if mid:
                out.append(self.get_message(mid, full=full, folder=folder))
        return out

    def list_messages(self, *, query: str = "", limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        limit = max(1, min(50, int(limit)))
        if folder:
            return self._list_one_folder(folder, query=query, limit=limit, full=full)
        folders = self.selected_folders()
        if not folders:
            return []
        # Fetch per configured folder so every returned row knows its source. Gmail
        # labels can overlap, therefore de-duplicate by message id after sorting.
        rows: list[MailMessage] = []
        per_folder = max(limit, min(50, limit * 2))
        for name in folders:
            rows.extend(self._list_one_folder(name, query=query, limit=per_folder, full=full))
        return _sort_messages(rows, limit)

    def _folder_status(self, folder: str) -> MailFolderStatus:
        base = self._scope_query(folder)
        now = int(time.time())
        midnight = _local_midnight_epoch()
        return MailFolderStatus(
            name=folder,
            total=self._count_query(base),
            unread=self._count_query(self._and_query(base, "is:unread")),
            recent24h=self._count_query(self._and_query(base, f"after:{now - 86400}")),
            today=self._count_query(self._and_query(base, f"after:{midnight}")),
            unread_today=self._count_query(self._and_query(base, f"is:unread after:{midnight}")),
        )

    def status(self, include_latest: int = 5) -> MailStatus:
        folders = self.selected_folders()
        folder_rows = [self._folder_status(name) for name in folders]
        scope = self._scope_query()
        now = int(time.time())
        midnight = _local_midnight_epoch()
        total = self._count_query(scope) if folders else 0
        unread = self._count_query(self._and_query(scope, "is:unread")) if folders else 0
        recent = self._count_query(self._and_query(scope, f"after:{now - 86400}")) if folders else 0
        today = self._count_query(self._and_query(scope, f"after:{midnight}")) if folders else 0
        unread_today = self._count_query(self._and_query(scope, f"is:unread after:{midnight}")) if folders and unread else 0
        latest = self.list_messages(limit=max(0, min(25, include_latest))) if include_latest and folders else []
        unread_latest = self.list_messages(query="is:unread", limit=min(5, max(0, include_latest))) if include_latest and unread else []
        account = self.account_email
        if not account:
            try:
                account = str(self.profile().get("emailAddress", "") or "")
            except Exception:
                pass
        return MailStatus(
            "gmail", total=total, unread=unread, recent24h=recent, today=today, unread_today=unread_today, account=account,
            latest=latest, unread_latest=unread_latest, folders=folder_rows,
        )

    def list_recent24h(self, *, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        return self.list_messages(query=f"after:{int(time.time()) - 86400}", limit=limit, full=full, folder=folder)

    def list_today(self, *, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        return self.list_messages(query=f"after:{_local_midnight_epoch()}", limit=limit, full=full, folder=folder)

    def list_unread_today(self, *, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        return self.list_messages(
            query=f"is:unread after:{_local_midnight_epoch()}", limit=limit, full=full, folder=folder
        )

    def list_unread_recent24h(self, *, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        return self.list_messages(
            query=f"is:unread after:{int(time.time()) - 86400}", limit=limit, full=full, folder=folder
        )

    def count_unread_today(self, *, folder: str | None = None) -> int:
        scope = self._scope_query(folder)
        if not self.selected_folders() and not folder:
            return 0
        return self._count_query(self._and_query(scope, f"is:unread after:{_local_midnight_epoch()}"))

    def count_unread_recent24h(self, *, folder: str | None = None) -> int:
        scope = self._scope_query(folder)
        if not self.selected_folders() and not folder:
            return 0
        return self._count_query(self._and_query(scope, f"is:unread after:{int(time.time()) - 86400}"))

    def send(self, to: str, subject: str, body: str, *, reply_to: MailMessage | None = None) -> dict[str, Any]:
        msg = EmailMessage()
        msg["To"] = to
        if self.account_email:
            msg["From"] = self.account_email
        msg["Subject"] = subject
        if reply_to and reply_to.rfc_message_id:
            msg["In-Reply-To"] = reply_to.rfc_message_id
            msg["References"] = reply_to.rfc_message_id
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes(policy=policy.SMTP)).decode("ascii").rstrip("=")
        payload: dict[str, Any] = {"raw": raw}
        if reply_to and reply_to.thread_id:
            payload["threadId"] = reply_to.thread_id
        return self._request("POST", "/users/me/messages/send", json=payload)

    def mark_read(self, message_id: str) -> None:
        self._request("POST", f"/users/me/messages/{message_id}/modify", json={"removeLabelIds": ["UNREAD"]})


def _imap_utf7_encode(value: str) -> str:
    """Encode an IMAP mailbox name using modified UTF-7 (RFC 3501)."""
    text = str(value or "")
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        raw = "".join(buf).encode("utf-16-be")
        enc = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append("&" + enc + "-")
        buf.clear()

    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _imap_utf7_decode(value: str) -> str:
    text = str(value or "")
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "&":
            out.append(text[i]); i += 1; continue
        j = text.find("-", i)
        if j < 0:
            out.append(text[i:]); break
        token = text[i + 1:j]
        if not token:
            out.append("&")
        else:
            try:
                raw = base64.b64decode(token.replace(",", "/") + "=" * (-len(token) % 4))
                out.append(raw.decode("utf-16-be"))
            except Exception:
                out.append(text[i:j + 1])
        i = j + 1
    return "".join(out)


def _imap_quote(value: str) -> str:
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mailru_pack_message_id(folder: str, uid: str) -> str:
    folder = str(folder or "Входящие")
    if folder.casefold() in {"входящие", "inbox"}:
        return str(uid)
    token = base64.urlsafe_b64encode(folder.encode("utf-8")).decode("ascii").rstrip("=")
    return f"f_{token}:{uid}"


def _mailru_unpack_message_id(message_id: str) -> tuple[str, str]:
    raw = str(message_id or "")
    if raw.startswith("f_") and ":" in raw:
        token, uid = raw[2:].split(":", 1)
        try:
            folder = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
            return folder, uid
        except Exception:
            pass
    return "Входящие", raw


class MailRuClient:
    def __init__(self, config) -> None:
        self.config = config
        self.account_email = str(config.get("integrations.mailru.email", "") or "").strip()
        self.password = unprotect_secret(str(config.get("integrations.mailru.app_password_encrypted", "") or ""))
        self.timeout = max(4.0, min(30.0, float(config.get("integrations.mailru.timeout_seconds", 10.0) or 10.0)))

    def configured(self) -> bool:
        return bool(self.account_email and self.password)

    def selected_folders(self) -> list[str]:
        return selected_mail_folders(self.config, "mailru")

    def _imap(self) -> imaplib.IMAP4_SSL:
        if not self.configured():
            raise RuntimeError("Mail.ru не настроен. Укажите адрес и пароль для внешнего приложения в API-сервисах.")
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout)
        try:
            client = imaplib.IMAP4_SSL(MAILRU_IMAP_HOST, MAILRU_IMAP_PORT)
            client.login(self.account_email, self.password)
            return client
        finally:
            socket.setdefaulttimeout(old_timeout)

    @staticmethod
    def _uids(client: imaplib.IMAP4_SSL, criteria: str | bytes, charset: str | None = None) -> list[str]:
        status, data = client.uid("search", charset, criteria)
        if status != "OK" or not data:
            return []
        return [x.decode("ascii", errors="ignore") for x in (data[0] or b"").split() if x]

    @staticmethod
    def _fetch_bytes(client: imaplib.IMAP4_SSL, uid: str, spec: str = "(BODY.PEEK[]<0.16384>)") -> bytes:
        status, data = client.uid("fetch", uid, spec)
        if status != "OK":
            return b""
        chunks: list[bytes] = []
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                chunks.append(bytes(item[1]))
        return b"\r\n".join(chunks)

    @staticmethod
    def _parse_list_mailbox(row: bytes | str) -> tuple[str, str]:
        text = row.decode("ascii", errors="replace") if isinstance(row, (bytes, bytearray)) else str(row or "")
        # LIST response ends with either a quoted mailbox name or an atom.
        m = re.search(r'("(?:[^"\\]|\\.)*"|\S+)\s*$', text)
        if not m:
            return "", ""
        raw = m.group(1).strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        return _imap_utf7_decode(raw), raw

    def list_folders(self) -> list[str]:
        client = self._imap()
        try:
            status, data = client.list()
            if status != "OK":
                return ["Входящие"]
            names: list[str] = []
            for row in data or []:
                display, _wire = self._parse_list_mailbox(row)
                if not display:
                    continue
                shown = "Входящие" if display.casefold() == "inbox" else display
                if shown.casefold() not in {x.casefold() for x in names}:
                    names.append(shown)
            if "входящие" not in {x.casefold() for x in names}:
                names.insert(0, "Входящие")
            return names
        finally:
            try: client.logout()
            except Exception: pass

    def _select_folder(self, client: imaplib.IMAP4_SSL, folder: str, *, readonly: bool) -> None:
        display = re.sub(r"\s+", " ", str(folder or "Входящие")).strip() or "Входящие"
        low = display.casefold()
        wire = "INBOX" if low in {"входящие", "inbox"} else _imap_utf7_encode(display)
        status, _data = client.select(_imap_quote(wire), readonly=readonly)
        if status == "OK":
            return
        # User may have entered a localized display name or a nested folder. Match
        # against the server LIST result case-insensitively before failing.
        list_status, rows = client.list()
        if list_status == "OK":
            for row in rows or []:
                shown, actual_wire = self._parse_list_mailbox(row)
                if shown.casefold() == display.casefold() or (display.casefold() == "входящие" and shown.casefold() == "inbox"):
                    status, _data = client.select(_imap_quote(actual_wire), readonly=readonly)
                    if status == "OK":
                        return
        raise RuntimeError(f"Mail.ru: папка «{display}» не найдена или недоступна по IMAP")

    def _parse_uid(self, client: imaplib.IMAP4_SSL, uid: str, *, folder: str, full: bool = False, unread_set: set[str] | None = None) -> MailMessage:
        spec = "(BODY.PEEK[])" if full else "(BODY.PEEK[]<0.16384>)"
        raw = self._fetch_bytes(client, uid, spec)
        msg = BytesParser(policy=policy.default).parsebytes(raw or b"\r\n")
        sender_raw = _decode_header(msg.get("From", ""))
        sender_name, sender_email = parseaddr(sender_raw)
        body = _message_body(msg, 10000 if full else 3000)
        date_value = str(msg.get("Date", "") or "")
        return MailMessage(
            provider="mailru",
            message_id=_mailru_pack_message_id(folder, uid),
            sender=_decode_header(sender_name) or sender_email or sender_raw,
            sender_email=sender_email,
            subject=_decode_header(msg.get("Subject", "")),
            preview=_preview(body),
            body=body if full else "",
            date=date_value,
            unread=uid in (unread_set or set()),
            rfc_message_id=str(msg.get("Message-ID", "") or ""),
            folder=folder,
            received_ts=_message_received_ts(date_value),
        )

    def _search_uids(self, client: imaplib.IMAP4_SSL, query: str) -> list[str]:
        criteria = "ALL"
        q = str(query or "").strip()
        if q and q.casefold() not in {"all", "in:inbox"}:
            low = q.casefold()
            if low.startswith("from:"):
                value = q.split(":", 1)[1].strip().replace('"', "")
                criteria = f'FROM "{value}"'
            elif low.startswith("subject:"):
                value = q.split(":", 1)[1].strip().replace('"', "")
                criteria = f'SUBJECT "{value}"'
            elif low in {"unread", "is:unread"}:
                criteria = "UNSEEN"
            else:
                value = q.replace('"', "")
                criteria = f'TEXT "{value}"'
        if any(ord(ch) > 127 for ch in str(criteria)):
            return self._uids(client, str(criteria).encode("utf-8", errors="replace"), "UTF-8")
        return self._uids(client, criteria)

    def _list_one_folder(self, folder: str, *, query: str = "", limit: int = 10, full: bool = False) -> list[MailMessage]:
        client = self._imap()
        try:
            self._select_folder(client, folder, readonly=True)
            unread = set(self._uids(client, "UNSEEN"))
            uids = self._search_uids(client, query)
            chosen = list(reversed(uids[-max(1, min(50, int(limit))):]))
            return [self._parse_uid(client, uid, folder=folder, full=full, unread_set=unread) for uid in chosen]
        finally:
            try: client.logout()
            except Exception: pass

    def list_messages(self, *, query: str = "", limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        limit = max(1, min(50, int(limit)))
        if folder:
            return self._list_one_folder(folder, query=query, limit=limit, full=full)
        rows: list[MailMessage] = []
        for name in self.selected_folders():
            rows.extend(self._list_one_folder(name, query=query, limit=max(limit, min(50, limit * 2)), full=full))
        return _sort_messages(rows, limit)

    def get_message(self, message_id: str, full: bool = True, *, folder: str = "") -> MailMessage:
        encoded_folder, uid = _mailru_unpack_message_id(message_id)
        target_folder = str(folder or encoded_folder or "Входящие")
        client = self._imap()
        try:
            self._select_folder(client, target_folder, readonly=True)
            unread = set(self._uids(client, "UNSEEN"))
            return self._parse_uid(client, str(uid), folder=target_folder, full=full, unread_set=unread)
        finally:
            try: client.logout()
            except Exception: pass

    def _uids_since_cutoff(self, client: imaplib.IMAP4_SSL, cutoff: datetime) -> list[str]:
        # IMAP SINCE is date-granular. Ask for a wider window, then use exact
        # INTERNALDATE timestamps so rolling-24h and local-calendar-today differ.
        start = (cutoff.astimezone() - timedelta(days=1)).strftime("%d-%b-%Y")
        candidates = self._uids(client, f'SINCE "{start}"')[-5000:]
        cutoff_utc = cutoff.astimezone(timezone.utc)
        by_uid: dict[str, datetime] = {}
        uid_rx = re.compile(rb'UID\s+(\d+)', re.I)
        date_rx = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.I)
        for offset in range(0, len(candidates), 400):
            batch = candidates[offset:offset + 400]
            if not batch:
                continue
            status, data = client.uid("fetch", ",".join(batch), "(UID INTERNALDATE)")
            if status != "OK":
                continue
            for item in data or []:
                raw = b""
                if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
                    raw = bytes(item[0])
                elif isinstance(item, (bytes, bytearray)):
                    raw = bytes(item)
                uid_match = uid_rx.search(raw)
                date_match = date_rx.search(raw)
                if not uid_match or not date_match:
                    continue
                try:
                    dt = datetime.strptime(date_match.group(1).decode("ascii", errors="ignore"), "%d-%b-%Y %H:%M:%S %z")
                except Exception:
                    continue
                if dt.astimezone(timezone.utc) >= cutoff_utc:
                    by_uid[uid_match.group(1).decode("ascii")] = dt.astimezone(timezone.utc)
        return [uid for uid, _dt in sorted(by_uid.items(), key=lambda pair: pair[1])]

    def _recent24_uids(self, client: imaplib.IMAP4_SSL) -> list[str]:
        return self._uids_since_cutoff(client, datetime.now(timezone.utc) - timedelta(hours=24))

    def _today_uids(self, client: imaplib.IMAP4_SSL) -> list[str]:
        local_now = datetime.now().astimezone()
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._uids_since_cutoff(client, local_midnight)

    def _folder_status(self, folder: str) -> MailFolderStatus:
        client = self._imap()
        try:
            self._select_folder(client, folder, readonly=True)
            all_uids = self._uids(client, "ALL")
            unread_uids = self._uids(client, "UNSEEN")
            recent = self._recent24_uids(client)
            today = self._today_uids(client)
            return MailFolderStatus(folder, total=len(all_uids), unread=len(unread_uids), recent24h=len(recent), today=len(today),
                                    unread_today=len(set(unread_uids).intersection(today)))
        finally:
            try: client.logout()
            except Exception: pass

    def list_recent24h(self, *, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        folders = [folder] if folder else self.selected_folders()
        rows: list[MailMessage] = []
        for name in folders:
            client = self._imap()
            try:
                self._select_folder(client, str(name), readonly=True)
                unread = set(self._uids(client, "UNSEEN"))
                uids = self._recent24_uids(client)
                chosen = list(reversed(uids[-max(1, min(50, int(limit))):]))
                rows.extend(self._parse_uid(client, uid, folder=str(name), full=full, unread_set=unread) for uid in chosen)
            finally:
                try: client.logout()
                except Exception: pass
        return _sort_messages(rows, limit)

    def list_today(self, *, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        folders = [folder] if folder else self.selected_folders()
        rows: list[MailMessage] = []
        for name in folders:
            client = self._imap()
            try:
                self._select_folder(client, str(name), readonly=True)
                unread = set(self._uids(client, "UNSEEN"))
                uids = self._today_uids(client)
                chosen = list(reversed(uids[-max(1, min(50, int(limit))):]))
                rows.extend(self._parse_uid(client, uid, folder=str(name), full=full, unread_set=unread) for uid in chosen)
            finally:
                try: client.logout()
                except Exception: pass
        return _sort_messages(rows, limit)

    def _list_unread_period(self, *, period: str, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        folders = [folder] if folder else self.selected_folders()
        rows: list[MailMessage] = []
        for name in folders:
            client = self._imap()
            try:
                self._select_folder(client, str(name), readonly=True)
                unread = set(self._uids(client, "UNSEEN"))
                period_uids = self._today_uids(client) if period == "today" else self._recent24_uids(client)
                chosen_ids = [uid for uid in period_uids if uid in unread]
                chosen = list(reversed(chosen_ids[-max(1, min(50, int(limit))):]))
                rows.extend(self._parse_uid(client, uid, folder=str(name), full=full, unread_set=unread) for uid in chosen)
            finally:
                try: client.logout()
                except Exception: pass
        return _sort_messages(rows, limit)

    def list_unread_today(self, *, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        return self._list_unread_period(period="today", limit=limit, full=full, folder=folder)

    def list_unread_recent24h(self, *, limit: int = 10, full: bool = False, folder: str | None = None) -> list[MailMessage]:
        return self._list_unread_period(period="recent24h", limit=limit, full=full, folder=folder)

    def status(self, include_latest: int = 5) -> MailStatus:
        folders = self.selected_folders()
        folder_rows: list[MailFolderStatus] = []
        errors: list[Exception] = []
        unavailable_folders: list[str] = []
        for name in folders:
            try:
                folder_rows.append(self._folder_status(name))
            except Exception as exc:
                errors.append(exc)
                unavailable_folders.append(str(name))
                from jarvis.runtime.briefing_stream import section_failure
                failure = section_failure(exc)
                logger.warning("Mail.ru folder status failed: error_type=%s reason=%s http_status=%s failure_kind=%s",
                               failure['error_type'], failure['reason'], failure.get('http_status'), failure['failure_kind'])
        latest = self.list_messages(limit=max(0, min(25, include_latest))) if include_latest and folder_rows else []
        unread_latest = self.list_messages(query="is:unread", limit=min(5, max(0, include_latest))) if include_latest and any(x.unread for x in folder_rows) else []
        if not folder_rows and folders and errors:
            raise errors[0]
        return MailStatus(
            "mailru",
            total=sum(x.total for x in folder_rows),
            unread=sum(x.unread for x in folder_rows),
            recent24h=sum(x.recent24h for x in folder_rows),
            today=sum(x.today for x in folder_rows),
            unread_today=sum(x.unread_today for x in folder_rows),
            account=self.account_email,
            latest=latest,
            unread_latest=unread_latest,
            folders=folder_rows,
            unavailable_folders=unavailable_folders,
        )

    def count_unread_today(self, *, folder: str | None = None) -> int:
        folders = [folder] if folder else self.selected_folders()
        total = 0
        for name in folders:
            client = self._imap()
            try:
                self._select_folder(client, str(name), readonly=True)
                unread = set(self._uids(client, "UNSEEN"))
                total += len(unread.intersection(self._today_uids(client)))
            finally:
                try: client.logout()
                except Exception: pass
        return total

    def count_unread_recent24h(self, *, folder: str | None = None) -> int:
        folders = [folder] if folder else self.selected_folders()
        total = 0
        for name in folders:
            client = self._imap()
            try:
                self._select_folder(client, str(name), readonly=True)
                unread = set(self._uids(client, "UNSEEN"))
                total += len(unread.intersection(self._recent24_uids(client)))
            finally:
                try: client.logout()
                except Exception: pass
        return total

    def send(self, to: str, subject: str, body: str, *, reply_to: MailMessage | None = None) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Mail.ru не настроен")
        msg = EmailMessage()
        msg["From"] = self.account_email
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to and reply_to.rfc_message_id:
            msg["In-Reply-To"] = reply_to.rfc_message_id
            msg["References"] = reply_to.rfc_message_id
        msg.set_content(body)
        with smtplib.SMTP_SSL(MAILRU_SMTP_HOST, MAILRU_SMTP_PORT, timeout=self.timeout) as smtp:
            smtp.login(self.account_email, self.password)
            smtp.send_message(msg)
        return {"sent": True, "provider": "mailru", "to": to}

    def mark_read(self, message_id: str) -> None:
        folder, uid = _mailru_unpack_message_id(message_id)
        client = self._imap()
        try:
            self._select_folder(client, folder, readonly=False)
            status, _ = client.uid("store", str(uid), "+FLAGS", "(\\Seen)")
            if status != "OK":
                raise RuntimeError("Mail.ru не подтвердил изменение флага письма")
        finally:
            try: client.logout()
            except Exception: pass


def provider_client(config, provider: str):
    p = _normalize_provider(provider)
    if p == "gmail":
        return GmailClient(config)
    if p == "mailru":
        return MailRuClient(config)
    if p == "auto":
        preferred = _normalize_provider(str(config.get("email.default_provider", "") or ""))
        order = [preferred] if preferred in {"gmail", "mailru"} else []
        order.extend(x for x in ("gmail", "mailru") if x not in order)
        for candidate in order:
            if candidate == "gmail" and bool(config.get("integrations.gmail.enabled", False)) and GmailClient(config).configured():
                return GmailClient(config)
            if candidate == "mailru" and bool(config.get("integrations.mailru.enabled", False)) and MailRuClient(config).configured():
                return MailRuClient(config)
        raise RuntimeError("Ни Gmail, ни Mail.ru не подключены")
    raise ValueError(f"Неизвестный почтовый сервис: {provider}")


def enabled_providers(config) -> list[str]:
    out: list[str] = []
    if bool(config.get("integrations.gmail.enabled", False)) and GmailClient(config).configured():
        out.append("gmail")
    if bool(config.get("integrations.mailru.enabled", False)) and MailRuClient(config).configured():
        out.append("mailru")
    return out


class MailConfigSnapshot188:
    """HIST-033: one immutable view of the settings for one mail collection cycle.

    Every provider/folder/count of a cycle reads the same settings, so a Settings
    Apply in the middle cannot mix old counts with a new folder list."""

    def __init__(self, config) -> None:
        data = getattr(config, "data", None)
        self._data = json.loads(json.dumps(data)) if isinstance(data, dict) else None
        self._source = config if self._data is None else None
        self.paths = getattr(config, "paths", None)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        if self._data is None:
            return self._source.get(dotted_key, default)
        value: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def notification_providers(config) -> list[str]:
    """HIST-033 policy: the background monitor polls only providers whose alerts are on."""
    return [p for p in enabled_providers(config)
            if bool(config.get(f"integrations.{p}.notifications_enabled", True))]


def collect_statuses(config, *, include_latest: int = 5, providers: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    # HOTFIX141: independent mail providers are queried concurrently. A daily
    # briefing now pays the latency of the slowest provider instead of the sum.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    config = MailConfigSnapshot188(config)
    allowed = None if providers is None else {_normalize_provider(p) for p in providers}
    providers = [p for p in enabled_providers(config) if allowed is None or p in allowed]
    result: dict[str, dict[str, Any]] = {}
    def _one(provider: str) -> tuple[str, dict[str, Any]]:
        try:
            status = provider_client(config, provider).status(include_latest=include_latest)
            return provider, status.compact()
        except Exception as exc:
            return provider, {"provider": provider, "error": str(exc), "latest": [], "unread_latest": []}
    if not providers:
        return result
    with ThreadPoolExecutor(max_workers=min(3, len(providers)), thread_name_prefix="NeoMailBrief") as pool:
        futures = [pool.submit(_one, p) for p in providers]
        for future in as_completed(futures):
            provider, payload = future.result()
            result[provider] = payload
    return result


def display_detail_for_provider(config, provider: str) -> str:
    p = _normalize_provider(provider)
    cfg = f"integrations.{p}"
    detail = str(
        config.get(f"{cfg}.message_display_detail", config.get(f"{cfg}.notification_detail", "sender_subject"))
        or "sender_subject"
    )
    allowed = {"count_only", "sender", "sender_subject", "sender_preview", "sender_subject_preview"}
    return detail if detail in allowed else "sender_subject"


def _message_detail_line(item: dict[str, Any], detail: str) -> str:
    sender = str(item.get("sender", "") or item.get("sender_email", "") or "Неизвестный отправитель")
    subject = str(item.get("subject", "") or "(без темы)")
    preview = _preview(str(item.get("preview", "") or item.get("body", "") or ""), 180)
    folder = str(item.get("folder", "") or "").strip()
    prefix = f"[{folder}] " if folder else ""
    if detail == "sender_subject":
        return f"{prefix}{sender} — {subject}"
    if detail == "sender_preview":
        return f"{prefix}{sender}" + (f": {preview}" if preview else "")
    if detail == "sender_subject_preview":
        base = f"{prefix}{sender} — {subject}"
        return base + (f"\n   {preview}" if preview else "")
    return f"{prefix}{sender}"


_NOTICE187 = {
    # total, unread, unread_today, recent24h, by_folder, folder, more, sender, subject
    'ru': ('Всего', 'Непрочитанных всего', 'Из них пришло сегодня', 'За 24 ч', 'По папкам', 'Папка', '… ещё {n}', 'Неизвестный отправитель', '(без темы)'),
    'en': ('Total', 'Unread in total', 'Of them arrived today', 'Last 24 h', 'By folder', 'Folder', '… {n} more', 'Unknown sender', '(no subject)'),
    'de': ('Gesamt', 'Ungelesen insgesamt', 'Davon heute eingegangen', 'Letzte 24 Std.', 'Nach Ordner', 'Ordner', '… {n} weitere', 'Unbekannter Absender', '(kein Betreff)'),
    'fr': ('Total', 'Non lus au total', 'Dont arrivés aujourd’hui', 'Dernières 24 h', 'Par dossier', 'Dossier', '… {n} de plus', 'Expéditeur inconnu', '(sans objet)'),
    'es': ('Total', 'Sin leer en total', 'De ellos, llegados hoy', 'Últimas 24 h', 'Por carpeta', 'Carpeta', '… {n} más', 'Remitente desconocido', '(sin asunto)'),
    'it': ('Totale', 'Non letti in totale', 'Di cui arrivati oggi', 'Ultime 24 h', 'Per cartella', 'Cartella', '… altri {n}', 'Mittente sconosciuto', '(senza oggetto)'),
    'pt': ('Total', 'Não lidos no total', 'Dos quais chegaram hoje', 'Últimas 24 h', 'Por pasta', 'Pasta', '… mais {n}', 'Remetente desconhecido', '(sem assunto)'),
}


def format_mail_notification(config, provider: str, status: dict[str, Any], new_messages: list[dict[str, Any]]) -> str:
    """NEO 187 (REC-SEMANTIC-SOURCE-01 / REC-REQ-MAIL-LAYOUT): the notification is
    built directly in the interface language. Counters are data and survive; the
    sender and subject are opaque user content and are never re-translated."""
    from jarvis.language import language_code
    code = language_code(config)
    words = _NOTICE187.get(code, _NOTICE187['en'])
    p = _normalize_provider(provider)
    prefix = "Gmail" if p == "gmail" else "Mail.ru"
    account = str(status.get("account", "") or "").strip()
    cfg = f"integrations.{p}"
    lines = [f"✉️ {prefix}" + (f" · {account}" if account else "")]
    if bool(config.get(f"{cfg}.notify_total_count", False)):
        lines.append(f"{words[0]}: {int(status.get('total', 0) or 0)}")
    if bool(config.get(f"{cfg}.notify_unread_count", True)):
        lines.append(f"{words[1]}: {int(status.get('unread', 0) or 0)}")
        if "unread_today" in status:
            lines.append(f"{words[2]}: {int(status.get('unread_today', 0) or 0)}")
    if bool(config.get(f"{cfg}.notify_recent24h_count", True)):
        lines.append(f"{words[3]}: {int(status.get('recent24h', 0) or 0)}")
    folders = [dict(x) for x in (status.get("folders", []) or []) if isinstance(x, dict)]
    if len(folders) > 1:
        lines.append(f"{words[4]}:")
        for folder in folders:
            folder_counts: list[str] = []
            if bool(config.get(f"{cfg}.notify_total_count", False)):
                folder_counts.append(f"{words[0]} {int(folder.get('total', 0) or 0)}")
            if bool(config.get(f"{cfg}.notify_unread_count", True)):
                folder_counts.append(f"{words[1]} {int(folder.get('unread', 0) or 0)}")
            if bool(config.get(f"{cfg}.notify_recent24h_count", True)):
                folder_counts.append(f"{words[3]} {int(folder.get('recent24h', 0) or 0)}")
            if folder_counts:
                lines.append(f"• {str(folder.get('name', '') or words[5])}: " + " · ".join(folder_counts))
    detail = display_detail_for_provider(config, p)
    if detail != "count_only":
        for item in new_messages[:5]:
            row = dict(item)
            if not str(row.get("sender", "") or row.get("sender_email", "") or "").strip():
                row["sender"] = words[7]
            if not str(row.get("subject", "") or "").strip():
                row["subject"] = words[8]
            lines.append("• " + _message_detail_line(row, detail))
        if len(new_messages) > 5:
            lines.append("• " + words[6].format(n=len(new_messages) - 5))
    return "\n".join(lines)


def format_mail_briefing(config, provider: str, status: dict[str, Any]) -> str:
    p = _normalize_provider(provider)
    prefix = "Gmail" if p == "gmail" else "Mail.ru"
    if status.get("error"):
        return f"{prefix}\n• Почта временно недоступна: {str(status.get('error', ''))[:180]}"
    lines = [
        prefix,
        f"• Непрочитанных: {int(status.get('unread', 0) or 0)}",
        f"• За последние 24 часа: {int(status.get('recent24h', 0) or 0)}",
    ]
    folders = [dict(x) for x in (status.get("folders", []) or []) if isinstance(x, dict)]
    if bool(config.get(f"integrations.{p}.briefing_show_folders", True)) and folders:
        lines.append("• По папкам:")
        for folder in folders:
            lines.append(
                f"  - {str(folder.get('name', '') or 'Папка')}: "
                f"непрочитанных {int(folder.get('unread', 0) or 0)} · "
                f"за 24 ч {int(folder.get('recent24h', 0) or 0)}"
            )
    if bool(config.get(f"integrations.{p}.briefing_show_senders", False)):
        senders: list[str] = []
        unread_rows = status.get("unread_latest", []) or status.get("latest", []) or []
        for item in unread_rows:
            if not bool(item.get("unread", False)):
                continue
            sender = str(item.get("sender", "") or item.get("sender_email", "") or "").strip()
            if sender and sender.casefold() not in {x.casefold() for x in senders}:
                senders.append(sender)
            if len(senders) >= 5:
                break
        if senders:
            lines.append("• Непрочитанные от: " + ", ".join(senders))
    return "\n".join(lines)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802
        params = parse_qs(urlparse(self.path).query)
        state = str((params.get("state") or [""])[0])
        if state != type(self).expected_state:
            type(self).result = {"error": "state_mismatch"}
            body = "NEO: ошибка проверки OAuth state. Вернитесь в приложение."
        elif params.get("error"):
            type(self).result = {"error": str(params["error"][0])}
            body = "NEO: авторизация Gmail отменена. Можно закрыть эту вкладку."
        else:
            type(self).result = {"code": str((params.get("code") or [""])[0])}
            body = "NEO: Gmail успешно авторизован. Можно закрыть эту вкладку и вернуться в настройки."
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def authorize_gmail(client_id: str, client_secret: str = "", *, timeout_seconds: int = 180) -> dict[str, Any]:
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    if not client_id:
        raise ValueError("Укажите OAuth Client ID типа Desktop app")
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    server.timeout = 0.5
    _OAuthCallbackHandler.result = {}
    _OAuthCallbackHandler.expected_state = state
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = GMAIL_AUTH_URL + "?" + urlencode(params)
    # 186: report the browser launch result instead of assuming it.
    if not webbrowser.open(url, new=2, autoraise=True):
        server.server_close()
        raise RuntimeError("Не удалось открыть браузер для входа в Google. Проверьте браузер по умолчанию и повторите.")
    deadline = time.time() + max(30, min(600, int(timeout_seconds)))
    try:
        while time.time() < deadline and not _OAuthCallbackHandler.result:
            server.handle_request()
    finally:
        server.server_close()
    result = dict(_OAuthCallbackHandler.result)
    if result.get("error"):
        raise RuntimeError(f"Gmail OAuth: {result['error']}")
    code = str(result.get("code", "") or "")
    if not code:
        raise TimeoutError("Время ожидания авторизации Gmail истекло")
    token_data: dict[str, Any] = {
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if client_secret:
        token_data["client_secret"] = client_secret
    response = requests.post(GMAIL_TOKEN_URL, data=token_data, timeout=15)
    if response.status_code >= 400:
        error = ""; description = ""
        try:
            payload = dict(response.json() or {})
            error = str(payload.get("error", "") or "")
            description = str(payload.get("error_description", "") or "")
        except Exception:
            pass
        if error == "invalid_grant":
            raise RuntimeError("Google OAuth: код авторизации истёк или уже использован. Нажмите «Авторизовать Gmail» ещё раз.")
        if error in {"invalid_client", "unauthorized_client"}:
            raise RuntimeError("Google OAuth: Client ID/Client secret не соответствуют Desktop app этого проекта.")
        detail = description or error or f"HTTP {response.status_code}"
        raise RuntimeError(f"Google OAuth: не удалось получить токен — {detail}")
    tokens = dict(response.json() or {})
    access = str(tokens.get("access_token", "") or "")
    if not access:
        raise RuntimeError("Google не вернул access token")
    profile_resp = requests.get(f"{GMAIL_API}/users/me/profile", headers={"Authorization": f"Bearer {access}"}, timeout=15)
    profile = dict(profile_resp.json() or {}) if profile_resp.ok else {}
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access,
        "refresh_token": str(tokens.get("refresh_token", "") or ""),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600) or 3600) - 30,
        "account_email": str(profile.get("emailAddress", "") or ""),
        "scope": str(tokens.get("scope", "") or ""),
    }


def test_gmail(client_id: str, client_secret: str, refresh_token: str, access_token: str = "", expires_at: float = 0.0) -> dict[str, Any]:
    class TempConfig:
        def __init__(self):
            self.values = {
                "integrations.gmail.client_id_encrypted": protect_secret(client_id) if client_id else "",
                "integrations.gmail.client_secret_encrypted": protect_secret(client_secret) if client_secret else "",
                "integrations.gmail.refresh_token_encrypted": protect_secret(refresh_token) if refresh_token else "",
                "integrations.gmail.access_token_encrypted": protect_secret(access_token) if access_token else "",
                "integrations.gmail.token_expires_at": expires_at,
                "integrations.gmail.timeout_seconds": 10.0,
            }
        def get(self, key: str, default=None): return self.values.get(key, default)
    client = GmailClient(TempConfig())
    profile = client.profile()
    return {"ok": True, "email": str(profile.get("emailAddress", "") or ""), "messages_total": int(profile.get("messagesTotal", 0) or 0)}


def test_mailru(email_address: str, app_password: str) -> dict[str, Any]:
    class TempConfig:
        def __init__(self):
            self.values = {
                "integrations.mailru.email": str(email_address or "").strip(),
                "integrations.mailru.app_password_encrypted": protect_secret(str(app_password or "")) if app_password else "",
                "integrations.mailru.timeout_seconds": 10.0,
            }
        def get(self, key: str, default=None): return self.values.get(key, default)
    client = MailRuClient(TempConfig())
    status = client.status(include_latest=0)
    # Verify SMTP authentication too.  No message is sent; this only checks that
    # the same external-app password is accepted by the official SMTP endpoint.
    with smtplib.SMTP_SSL(MAILRU_SMTP_HOST, MAILRU_SMTP_PORT, timeout=client.timeout) as smtp:
        smtp.login(client.account_email, client.password)
    return {
        "ok": True, "email": status.account, "total": status.total,
        "unread": status.unread, "imap": True, "smtp": True,
    }
