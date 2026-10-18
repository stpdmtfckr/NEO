# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Official Google Calendar integration for NEO.

Uses the OAuth 2.0 installed/Desktop application flow with PKCE and the Calendar
REST API.  Tokens are stored by ConfigManager only after being DPAPI protected.
The integration intentionally uses calendar events, not NEO's timer/reminder
runtime, so the two domains cannot steal each other's commands.
"""

from datetime import datetime, timedelta, time as dt_time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, quote
import base64
import hashlib
import re
import secrets
import threading
import time
import webbrowser

import requests

from jarvis.secrets import unprotect_secret

CALENDAR_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CALENDAR_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _reads_cache():
    from jarvis.runtime.http_pool173 import ResponseCache
    return ResponseCache(ttl=60.0, limit=64)


_CALENDAR_READS = _reads_cache()
_TOKEN_LOCK = threading.Lock()


class _OAuthHandler(BaseHTTPRequestHandler):
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
            body = "NEO: авторизация Google Calendar отменена. Можно закрыть вкладку."
        else:
            type(self).result = {"code": str((params.get("code") or [""])[0])}
            body = "NEO: Google Calendar подключён. Можно закрыть вкладку и вернуться в настройки."
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _secret(config: Any, key: str) -> str:
    return unprotect_secret(str(config.get(key, "") or "")).strip()


def calendar_oauth_client(config: Any) -> tuple[str, str]:
    """Calendar and Gmail intentionally share one Desktop OAuth client.

    Calendar keeps its own refresh token/scopes, but HOTFIX96 removes duplicate
    Client ID/secret fields and always reads the Gmail OAuth application pair.
    """
    return (
        _secret(config, "integrations.gmail.client_id_encrypted"),
        _secret(config, "integrations.gmail.client_secret_encrypted"),
    )


def authorize_google_calendar(client_id: str, client_secret: str = "", *, timeout_seconds: int = 180) -> dict[str, Any]:
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    if not client_id:
        raise ValueError("Укажите OAuth Client ID типа Desktop app в разделе «Gmail и Календарь»")
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", 0), _OAuthHandler)
    server.timeout = 0.5
    _OAuthHandler.result = {}
    _OAuthHandler.expected_state = state
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
    url = CALENDAR_AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(CALENDAR_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    # 186: report the browser launch result instead of assuming it.
    if not webbrowser.open(url, new=2, autoraise=True):
        server.server_close()
        raise RuntimeError("Не удалось открыть браузер для входа в Google. Проверьте браузер по умолчанию и повторите.")
    deadline = time.time() + max(30, min(600, int(timeout_seconds)))
    try:
        while time.time() < deadline and not _OAuthHandler.result:
            server.handle_request()
    finally:
        server.server_close()
    result = dict(_OAuthHandler.result)
    if result.get("error"):
        raise RuntimeError(f"Google Calendar OAuth: {result['error']}")
    code = str(result.get("code", "") or "")
    if not code:
        raise TimeoutError("Время ожидания авторизации Google Calendar истекло")
    payload: dict[str, Any] = {
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if client_secret:
        payload["client_secret"] = client_secret
    response = requests.post(CALENDAR_TOKEN_URL, data=payload, timeout=15)
    if response.status_code >= 400:
        detail = ""
        try:
            body = dict(response.json() or {})
            detail = str(body.get("error_description", "") or body.get("error", "") or "")
        except Exception:
            pass
        raise RuntimeError(f"Google Calendar OAuth HTTP {response.status_code}" + (f": {detail}" if detail else ""))
    tokens = dict(response.json() or {})
    access = str(tokens.get("access_token", "") or "")
    if not access:
        raise RuntimeError("Google не вернул access token для Calendar")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access,
        "refresh_token": str(tokens.get("refresh_token", "") or ""),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600) or 3600) - 30,
        "scope": str(tokens.get("scope", "") or ""),
    }


class GoogleCalendarClient:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.timeout = max(3.0, min(30.0, float(config.get("integrations.google_calendar.timeout_seconds", 12.0) or 12.0)))
        # NEO 173: one Calendar session for the whole NEO process (clients are created per command).
        from jarvis.runtime import http_pool173
        self._session = http_pool173.session("calendar")
        self._oauth_cache_key: tuple[str, str, str, str, float] | None = None
        self._oauth_cache_value: tuple[str, str, str, str, float] | None = None

    def _oauth(self) -> tuple[str, str, str, str, float]:
        # DPAPI decrypts are comparatively expensive and this method is on every
        # Calendar API request. Cache decrypted values while still observing live
        # Settings changes by keying the cache on the encrypted source strings.
        client_id_enc = str(self.config.get("integrations.gmail.client_id_encrypted", "") or "")
        client_secret_enc = str(self.config.get("integrations.gmail.client_secret_encrypted", "") or "")
        refresh_enc = str(self.config.get("integrations.google_calendar.refresh_token_encrypted", "") or "")
        access_enc = str(self.config.get("integrations.google_calendar.access_token_encrypted", "") or "")
        expires = float(self.config.get("integrations.google_calendar.token_expires_at", 0.0) or 0.0)
        cache_key = (client_id_enc, client_secret_enc, refresh_enc, access_enc, expires)
        if self._oauth_cache_key == cache_key and self._oauth_cache_value is not None:
            return self._oauth_cache_value
        value = (
            unprotect_secret(client_id_enc).strip(),
            unprotect_secret(client_secret_enc).strip(),
            unprotect_secret(refresh_enc).strip(),
            unprotect_secret(access_enc).strip(),
            expires,
        )
        self._oauth_cache_key = cache_key
        self._oauth_cache_value = value
        return value

    def configured(self) -> bool:
        client_id, _secret_value, refresh, _access, _expires = self._oauth()
        return bool(self.config.get("integrations.google_calendar.enabled", False) and client_id and refresh)

    def _access_token(self, *, force: bool = False) -> str:
        client_id, client_secret, refresh, access, expires = self._oauth()
        if not client_id or not refresh:
            raise RuntimeError("Google Calendar не авторизован")
        cache_key = hashlib.sha256((client_id + "\0" + refresh).encode("utf-8")).hexdigest()
        now = time.time()
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(cache_key)
            if not force and cached and cached[1] > now + 20:
                return cached[0]
        if not force and access and expires > now + 30:
            with _TOKEN_LOCK:
                _TOKEN_CACHE[cache_key] = (access, expires)
            return access
        data: dict[str, str] = {
            "client_id": client_id,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
        if client_secret:
            data["client_secret"] = client_secret
        response = self._session.post(CALENDAR_TOKEN_URL, data=data, timeout=self.timeout)
        if response.status_code >= 400:
            detail = ""
            try:
                body = dict(response.json() or {})
                detail = str(body.get("error_description", "") or body.get("error", "") or "")
            except Exception:
                pass
            raise RuntimeError(f"Google Calendar OAuth refresh: HTTP {response.status_code}")
        body = dict(response.json() or {})
        token = str(body.get("access_token", "") or "")
        if not token:
            raise RuntimeError("Google Calendar: пустой access token")
        expiry = now + int(body.get("expires_in", 3600) or 3600) - 30
        with _TOKEN_LOCK:
            _TOKEN_CACHE[cache_key] = (token, expiry)
        return token

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> Any:
        if not self.config.get("integrations.google_calendar.enabled", False):
            raise RuntimeError("Google Calendar отключён в настройках")
        # NEO 173: the same event list asked again within a minute comes from memory;
        # creating, changing or deleting an event through NEO clears it.
        from jarvis.runtime import http_pool173
        import copy
        # Only NEO's own shared session is cached: a status check or an injected session always
        # talks to the server.
        cache = self._session is http_pool173.session("calendar")
        if method.upper() != "GET":
            _CALENDAR_READS.clear()
        elif cache:
            key = http_pool173.ResponseCache.key(method, path, params, self._oauth()[2][-24:])
            cached = _CALENDAR_READS.get(key)
            if cached is not None:
                return copy.deepcopy(cached)
        with http_pool173.timed("calendar", method.upper()):
            result = self._request_uncached(method, path, params=params, json_body=json_body)
        if method.upper() == "GET" and cache:
            _CALENDAR_READS.put(key, copy.deepcopy(result))
        return result

    def _request_uncached(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> Any:
        token = self._access_token()
        response = self._session.request(
            method,
            CALENDAR_API + path,
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code == 401 and method.upper() == 'GET':
            token = self._access_token(force=True)
            response = self._session.request(
                method, CALENDAR_API + path, params=params, json=json_body,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=self.timeout,
            )
        if response.status_code >= 400:
            detail = ""
            reason = ""
            try:
                err = dict((response.json() or {}).get("error") or {})
                detail = str(err.get("message", "") or "")
                errors = err.get("errors") or []
                if errors and isinstance(errors[0], dict):
                    reason = str(errors[0].get("reason", "") or "")
            except Exception:
                pass
            if response.status_code == 403 and ("accessNotConfigured" in reason or "has not been used" in detail or "disabled" in detail):
                raise RuntimeError("Google Calendar API не включён в Google Cloud-проекте. Включите Google Calendar API и повторите проверку.")
            raise RuntimeError(f"Google Calendar HTTP {response.status_code}" + (f": {detail}" if detail else ""))
        if response.status_code == 204:
            return {}
        return response.json() if response.content else {}

    @staticmethod
    def _local_bounds(day_offset: int = 0) -> tuple[datetime, datetime]:
        now = datetime.now().astimezone()
        day = (now + timedelta(days=day_offset)).date()
        start = datetime.combine(day, dt_time.min, tzinfo=now.tzinfo)
        return start, start + timedelta(days=1)

    def list_events(self, *, start: datetime | None = None, end: datetime | None = None, query: str = "", limit: int = 25) -> list[dict[str, Any]]:
        if start is None:
            start = datetime.now().astimezone()
        params: dict[str, Any] = {
            "timeMin": start.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max(1, min(100, int(limit))),
        }
        if end is not None:
            params["timeMax"] = end.isoformat()
        if str(query or "").strip():
            params["q"] = str(query).strip()
        data = dict(self._request("GET", "/calendars/primary/events", params=params) or {})
        return [dict(x) for x in (data.get("items") or []) if isinstance(x, dict) and str(x.get("status", "")) != "cancelled"]

    def today(self, *, offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        start, end = self._local_bounds(offset)
        return self.list_events(start=start, end=end, limit=limit)

    def upcoming(self, *, days: int = 7, limit: int = 50) -> list[dict[str, Any]]:
        start = datetime.now().astimezone()
        return self.list_events(start=start, end=start + timedelta(days=max(1, min(365, int(days)))), limit=limit)

    def next_event(self) -> dict[str, Any] | None:
        rows = self.upcoming(days=30, limit=1)
        return rows[0] if rows else None

    def insert_event(self, *, title: str, start: datetime, duration_minutes: int = 60, description: str = "", location: str = "") -> dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            raise ValueError("Не указано название события")
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.now().astimezone().tzinfo)
        end = start + timedelta(minutes=max(5, min(1440, int(duration_minutes or 60))))
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if description:
            body["description"] = str(description)
        if location:
            body["location"] = str(location)
        return dict(self._request("POST", "/calendars/primary/events", json_body=body) or {})

    def delete_event(self, event_id: str) -> None:
        if not event_id:
            raise ValueError("Не указан event_id")
        self._request("DELETE", f"/calendars/primary/events/{quote(str(event_id), safe='')}")

    def update_event(self, event_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        if not event_id:
            raise ValueError("Не указан event_id")
        return dict(self._request("PATCH", f"/calendars/primary/events/{quote(str(event_id), safe='')}", json_body=dict(changes or {})) or {})


def _event_start(event: dict[str, Any]) -> tuple[str, datetime | None]:
    raw = dict(event.get("start") or {})
    if raw.get("dateTime"):
        value = str(raw.get("dateTime") or "")
        try:
            return value, datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except Exception:
            return value, None
    if raw.get("date"):
        value = str(raw.get("date") or "")
        try:
            return value, datetime.fromisoformat(value).astimezone()
        except Exception:
            return value, None
    return "", None


# PHRASE185-L10N-01: service words of the calendar reply (headings, «no events»,
# «untitled») follow the UI language; the user's own titles/places stay literal.
_CAL_WORDS = {
    #            no events        untitled            no time               when          where        description      connected
    "ru": ("Событий нет.", "(без названия)", "время не указано", "Когда", "Где", "Описание", "Google Calendar подключён."),
    "en": ("No events.", "(untitled)", "time not set", "When", "Where", "Description", "Google Calendar connected."),
    "de": ("Keine Termine.", "(ohne Titel)", "keine Uhrzeit", "Wann", "Wo", "Beschreibung", "Google Calendar verbunden."),
    "fr": ("Aucun événement.", "(sans titre)", "heure non indiquée", "Quand", "Où", "Description", "Google Agenda connecté."),
    "es": ("No hay eventos.", "(sin título)", "hora sin indicar", "Cuándo", "Dónde", "Descripción", "Google Calendar conectado."),
    "it": ("Nessun evento.", "(senza titolo)", "ora non indicata", "Quando", "Dove", "Descrizione", "Google Calendar collegato."),
    "pt": ("Sem eventos.", "(sem título)", "hora não indicada", "Quando", "Onde", "Descrição", "Google Calendar ligado."),
}
_CAL_HEADINGS = {
    "schedule": ("Расписание", "Schedule", "Zeitplan", "Planning", "Agenda", "Programma", "Agenda"),
    "today": ("Расписание на сегодня", "Today’s schedule", "Heutiger Zeitplan", "Planning d’aujourd’hui", "Agenda de hoy", "Programma di oggi", "Agenda de hoje"),
    "tomorrow": ("Расписание на завтра", "Tomorrow’s schedule", "Zeitplan für morgen", "Planning de demain", "Agenda de mañana", "Programma di domani", "Agenda de amanhã"),
    "upcoming": ("Ближайшие события", "Upcoming events", "Bevorstehende Termine", "Événements à venir", "Próximos eventos", "Prossimi eventi", "Próximos eventos"),
    "all": ("Всё ближайшее расписание", "Upcoming schedule", "Kommender Zeitplan", "Planning à venir", "Próxima agenda", "Programma in arrivo", "Próxima agenda"),
    "next": ("Следующее событие", "Next event", "Nächster Termin", "Prochain événement", "Próximo evento", "Prossimo evento", "Próximo evento"),
}
_CAL_ORDER = ("ru", "en", "de", "fr", "es", "it", "pt")


def _ru_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def calendar_heading(key: str, lang: str = "ru", days: int | None = None) -> str:
    lang = lang if lang in _CAL_ORDER else "ru"
    if key == "days":
        n = max(1, int(days or 1))
        return {
            "ru": f"Расписание на {n} {_ru_days(n)}" if n != 1 else "Расписание на 1 день",
            "en": f"Schedule for the next {n} days" if n != 1 else "Schedule for the next day",
            "de": f"Zeitplan für die nächsten {n} Tage" if n != 1 else "Zeitplan für den nächsten Tag",
            "fr": f"Planning des {n} prochains jours" if n != 1 else "Planning du prochain jour",
            "es": f"Agenda de los próximos {n} días" if n != 1 else "Agenda del próximo día",
            "it": f"Programma dei prossimi {n} giorni" if n != 1 else "Programma del prossimo giorno",
            "pt": f"Agenda dos próximos {n} dias" if n != 1 else "Agenda do próximo dia",
        }[lang]
    row = _CAL_HEADINGS.get(key)
    return row[_CAL_ORDER.index(lang)] if row else key


def calendar_connected(lang: str = "ru") -> str:
    return _CAL_WORDS.get(lang, _CAL_WORDS["ru"])[6]


def format_calendar_events(rows: list[dict[str, Any]], *, heading: str = "Расписание", lang: str = "ru") -> str:
    """Render calendar data as distinct event blocks instead of a dense sentence."""
    words = _CAL_WORDS.get(lang, _CAL_WORDS["ru"])
    if not rows:
        return f"{heading}\n\n• {words[0]}"
    lines = [heading, ""]
    for index, item in enumerate(rows, 1):
        title = str(item.get("summary", "") or words[1])
        raw, dt = _event_start(item)
        if dt is not None:
            when = dt.strftime("%d.%m.%Y %H:%M") if "T" in raw else dt.strftime("%d.%m.%Y")
        else:
            when = raw or words[2]
        location = str(item.get("location", "") or "").strip()
        description = str(item.get("description", "") or "").strip()
        sep = " : " if lang == "fr" else ": "
        lines.append(f"{index}. {title}")
        lines.append(f"   {words[3]}{sep}{when}")
        if location:
            lines.append(f"   {words[4]}{sep}{location}")
        if description:
            compact = " ".join(description.split())
            lines.append(f"   {words[5]}{sep}{compact[:240]}")
        if index != len(rows):
            lines.append("")
    return "\n".join(lines)


def parse_calendar_datetime(date_text: str, time_text: str = "") -> datetime:
    raw = (str(date_text or "").strip() + " " + str(time_text or "").strip()).strip()
    if not raw:
        raise ValueError("Не указаны дата и время")
    try:
        import dateparser
        dt = dateparser.parse(raw, languages=["ru", "en"], settings={
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "DMY",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": str(datetime.now().astimezone().tzinfo),
        })
    except Exception:
        dt = None
    if dt is None:
        low = raw.casefold().replace("ё", "е")
        now = datetime.now().astimezone()
        offset = 0 if "сегодня" in low else 1 if "завтра" in low else 2 if "послезавтра" in low else None
        dmatch = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", low)
        tmatch = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", low)
        if offset is not None:
            day = (now + timedelta(days=offset)).date()
        elif dmatch:
            year = int(dmatch.group(3) or now.year)
            if year < 100:
                year += 2000
            day = datetime(year, int(dmatch.group(2)), int(dmatch.group(1))).date()
        else:
            raise ValueError("Не удалось распознать дату")
        hour, minute = (int(tmatch.group(1)), int(tmatch.group(2))) if tmatch else (9, 0)
        dt = datetime.combine(day, dt_time(hour, minute), tzinfo=now.tzinfo)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone()


def test_google_calendar(config: Any) -> dict[str, Any]:
    client = GoogleCalendarClient(config)
    rows = client.upcoming(days=7, limit=3)
    return {"ok": True, "events": len(rows), "sample": rows}


_CALENDAR_ADD_VERBS = r"(?:добавь|добавить|внеси|внести|создай|создать|запланируй|запланировать|поставь|поставить|запиши|записать)"
_CALENDAR_SHELL = r"(?:в\s+(?:расписани\w*|календар\w*)|событи\w*\s+в\s+(?:расписани\w*|календар\w*))"
_DATE_TOKEN = r"(?:сегодня|завтра|послезавтра|\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{4}-\d{1,2}-\d{1,2})"
_TIME_TOKEN = r"(?:[01]?\d|2[0-3])[:.]\d{2}"


def parse_calendar_add_command(command: str) -> dict[str, str]:
    """Extract title/date/time independent of their order.

    Command-shell words are removed before the title is derived, so phrases
    such as ``добавь в расписание 26.08.2026 в 18:00 купить расчёску`` store
    ``купить расчёску`` rather than ``в расписание``.
    """
    raw=str(command or "").strip()
    low=raw.casefold().replace("ё","е")
    if not re.search(rf"\b{_CALENDAR_ADD_VERBS}\b", low, re.I):
        raise ValueError("Команда не похожа на добавление события")
    dm=re.search(rf"\b(?P<date>{_DATE_TOKEN})\b", low, re.I)
    tm=re.search(rf"\b(?P<time>{_TIME_TOKEN})\b", low, re.I)
    if not dm:
        raise ValueError("Не удалось определить дату события")
    if not tm:
        raise ValueError("Не удалось определить время события")
    # Work on the original string with same-width matches where possible.
    text=raw
    # Strip wake word and leading add shell.
    text=re.sub(r"^\s*(?:нео|нио|нэо|neo|nio|джарвис|jarvis)\b\s*[,!:.;—-]*\s*", "", text, flags=re.I)
    text=re.sub(rf"^\s*{_CALENDAR_ADD_VERBS}\b\s*", "", text, count=1, flags=re.I)
    # Remove command namespace, but not meaningful title words such as
    # "встречу с Иваном".
    text=re.sub(r"^\s*(?:событи\w*\s+)?(?:в\s+)?(?:расписани\w*|календар\w*)\b\s*[,;:-]*\s*", "", text, count=1, flags=re.I)
    text=re.sub(r"\b(?:в|на)\s+(?:расписани\w*|календар\w*)\b", " ", text, flags=re.I)
    # Remove concrete date and time tokens wherever they occur, including
    # their local prepositions/labels.
    date_value=dm.group("date")
    time_value=tm.group("time").replace(".",":")
    text=re.sub(rf"(?:\b(?:на|дата|дату)\s+)?\b{re.escape(date_value)}\b", " ", text, flags=re.I)
    text=re.sub(rf"(?:\b(?:в|на|время)\s+)?\b{re.escape(tm.group('time'))}\b", " ", text, flags=re.I)
    # Common glue remaining after shell/date removal.
    text=re.sub(r"^\s*(?:событи\w*\s*[:,-]?\s*)", "", text, count=1, flags=re.I)
    text=re.sub(r"\s+", " ", text).strip(" \t\r\n,;:—-")
    if not text:
        raise ValueError("Не удалось определить название события")
    return {"title":text,"date":date_value,"time":time_value}


def event_start_datetime(event: dict[str, Any]) -> datetime | None:
    return _event_start(event)[1]


def event_end_datetime(event: dict[str, Any]) -> datetime | None:
    raw=dict(event.get("end") or {})
    value=str(raw.get("dateTime","") or raw.get("date","") or "")
    if not value: return None
    try: return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone()
    except Exception: return None


def calendar_patch_changes(event: dict[str, Any], *, title: str = "", date_text: str = "", time_text: str = "") -> dict[str, Any]:
    changes: dict[str, Any]={}
    if str(title or "").strip(): changes["summary"]=str(title).strip()
    if str(date_text or "").strip() or str(time_text or "").strip():
        old_start=event_start_datetime(event) or datetime.now().astimezone()
        old_end=event_end_datetime(event) or (old_start+timedelta(hours=1))
        duration=max(timedelta(minutes=5),old_end-old_start)
        date_part=str(date_text or old_start.strftime("%d.%m.%Y"))
        time_part=str(time_text or old_start.strftime("%H:%M"))
        new_start=parse_calendar_datetime(date_part,time_part)
        changes["start"]={"dateTime":new_start.isoformat()}
        changes["end"]={"dateTime":(new_start+duration).isoformat()}
    return changes
