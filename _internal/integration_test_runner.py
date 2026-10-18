"""Explicit interactive integration QA: read-only API probes, ephemeral secrets."""
from __future__ import annotations
from dataclasses import dataclass
import getpass
import imaplib
from pathlib import Path
import sys
import time
import webbrowser
from bootstrap import ReadOnlyConfig, DATA
from jarvis.runtime.diagnostics import write_report


@dataclass(frozen=True)
class Provider:
    name: str
    enabled_key: str
    secret_key: str
    page: str
    endpoint: str
    credential_type: str
    params: tuple = ()


PROVIDERS = (
    Provider("SearchAPI", "gaming.web_knowledge.enabled", "gaming.web_knowledge.searchapi_api_key_encrypted", "https://www.searchapi.io/", "https://www.searchapi.io/api/v1/search", "api_key", (("engine", "google"), ("q", "NEO desktop assistant integration test"))),
    Provider("Yandex Music", "integrations.yandex_music.enabled", "integrations.yandex_music.token_encrypted", "https://music.yandex.ru/", "https://api.music.yandex.net/account/status", "OAuth"),
    Provider("Telegram", "integrations.telegram.enabled", "integrations.telegram.bot_token_encrypted", "https://t.me/BotFather", "https://api.telegram.org/bot{credential}/getMe", "telegram"),
    Provider("Discord", "integrations.discord.enabled", "integrations.discord.bot_token_encrypted", "https://discord.com/developers/applications", "https://discord.com/api/v10/users/@me", "Bot"),
    Provider("Spotify", "integrations.spotify.enabled", "integrations.spotify.access_token_encrypted", "https://developer.spotify.com/dashboard", "https://api.spotify.com/v1/me", "Bearer"),
    Provider("Gmail", "integrations.gmail.enabled", "integrations.gmail.access_token_encrypted", "https://myaccount.google.com/", "https://gmail.googleapis.com/gmail/v1/users/me/profile", "Bearer"),
    Provider("Google Calendar", "integrations.google_calendar.enabled", "integrations.google_calendar.access_token_encrypted", "https://calendar.google.com/", "https://www.googleapis.com/calendar/v3/users/me/calendarList", "Bearer", (("maxResults", "1"),)),
    Provider("OpenWeather", "integrations.openweather.enabled", "integrations.openweather.api_key_encrypted", "https://home.openweathermap.org/api_keys", "https://api.openweathermap.org/data/2.5/weather", "appid", (("q", "London"),)),
    Provider("YouTube", "integrations.youtube.enabled", "integrations.youtube.api_key_encrypted", "https://console.cloud.google.com/apis/credentials", "https://www.googleapis.com/youtube/v3/videos", "key", (("part", "id"), ("id", "jNQXAC9IVRw"))),
)


def probe(provider: Provider, credential: str, session) -> dict:
    """Never return a URL, token, response body, account ID, or exception message."""
    started = time.monotonic()
    headers, params = {}, dict(provider.params)
    endpoint = provider.endpoint
    if provider.credential_type in {"Bearer", "OAuth", "Bot"}:
        headers["Authorization"] = provider.credential_type + " " + credential
    elif provider.credential_type == "telegram":
        # A bot token must be one bounded URL-safe segment, never a URL override.
        import re
        if not re.fullmatch(r"\d{5,14}:[A-Za-z0-9_-]{20,100}", credential):
            return {"status": "FAIL", "reason": "Invalid bot token format"}
        endpoint = endpoint.format(credential=credential)
    else:
        params[provider.credential_type] = credential
    try:
        response = session.get(endpoint, params=params, headers=headers, timeout=(4, 8), allow_redirects=False)
        code = int(response.status_code)
        status = "PASS" if 200 <= code < 300 else "UNAVAILABLE" if code in {401, 403, 429} else "FAIL"
        if status == "PASS":
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
                status = "UNAVAILABLE"
            elif provider.name == "Telegram" and not payload.get("ok"):
                status = "UNAVAILABLE"
        return {"status": status, "http_status": code, "seconds": round(time.monotonic() - started, 3)}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": type(exc).__name__, "seconds": round(time.monotonic() - started, 3)}


def _credential(config, key):
    from jarvis.secrets import unprotect_secret
    protected = str(config.get(key, "") or "")
    try:
        return str(unprotect_secret(protected) if protected else "").strip()
    except Exception:
        return ""


def main() -> int:
    if not sys.stdin.isatty():
        print("Integration QA requires an interactive terminal: NEO.cmd --integration-test")
        return 2
    import requests
    config = ReadOnlyConfig()
    print("NEO interactive integration QA")
    print("Only read-only authentication/status requests are made. SearchAPI/YouTube/weather may use one request of your API quota.")
    print("Messages, music playback, files and screenshots are not sent. Entered keys are used only for this session and never saved.")
    if input("Run enabled integration checks? [y/N]: ").strip().casefold() not in {"y", "yes", "д", "да"}:
        return 0
    results = []
    with requests.Session() as session:
        session.trust_env = True  # Respect the user's configured corporate proxy.
        for provider in PROVIDERS:
            row = {"provider": provider.name}
            if not bool(config.get(provider.enabled_key, False)):
                row["status"] = "DISABLED"
                results.append(row)
                continue
            credential = _credential(config, provider.secret_key)
            if not credential:
                action = input(f"{provider.name}: no saved credential. [o] official page, [k] temporary key/token, [Enter] skip: ").strip().casefold()
                if action == "o":
                    webbrowser.open(provider.page)
                    print("Complete connection in NEO Settings; no token is stored by this test.")
                    row.update(status="SKIP", reason="Official connection page opened by user request")
                    results.append(row)
                    continue
                if action == "k":
                    credential = getpass.getpass(f"{provider.name} temporary credential (hidden): ").strip()
            if not credential:
                row.update(status="SKIP", reason="Credential unavailable")
            else:
                row.update(probe(provider, credential, session))
            credential = ""
            print(f"{provider.name}: {row['status']}" + (f" (HTTP {row['http_status']})" if "http_status" in row else ""))
            results.append(row)
    if bool(config.get("integrations.mailru.enabled", False)):
        row = {"provider": "Mail.ru"}
        password = _credential(config, "integrations.mailru.app_password_encrypted")
        account = str(config.get("integrations.mailru.email", "") or "")
        if not account or not password:
            if input("Mail.ru: no complete saved credentials. Open official account page? [y/N]: ").strip().casefold() in {"y", "yes", "да"}:
                webbrowser.open("https://account.mail.ru/")
            row.update(status="SKIP", reason="Connect app password in NEO Settings")
        else:
            try:
                with imaplib.IMAP4_SSL("imap.mail.ru", 993, timeout=10) as mail:
                    mail.login(account, password)
                    kind, _ = mail.status("INBOX", "(MESSAGES)")
                    row["status"] = "PASS" if kind == "OK" else "UNAVAILABLE"
            except Exception as exc:
                row.update(status="UNAVAILABLE", reason=type(exc).__name__)
        password = ""
        results.append(row)
        print(f"Mail.ru: {row['status']}")
    path = write_report(DATA / "diagnostics" / "integration_test_latest.json", {"generated_at": time.time(), "scope": "interactive read-only provider reachability/authentication; not playback or message dispatch", "results": results})
    print(f"Sanitized report: {path}")
    return 1 if any(x["status"] == "FAIL" for x in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
