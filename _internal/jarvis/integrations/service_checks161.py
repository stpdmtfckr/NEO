"""On-demand, read-only service checks for the streamed daily briefing.

No startup polling timer, message dispatch, player control, browser authorization
or raw provider response is involved. OAuth refresh uses the production clients.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_STATUS = {
    'ok': ('отвечает.', 'responding.', 'antwortet.', 'répond.', 'responde.', 'risponde.', 'responde.'),
    'configuration': ('нужно проверить подключение в настройках.', 'check its connection in Settings.', 'Verbindung in den Einstellungen prüfen.', 'vérifiez sa connexion dans les paramètres.', 'revisa la conexión en los ajustes.', 'controlla la connessione nelle impostazioni.', 'verifique a ligação nas definições.'),
    'authentication': ('нужно проверить авторизацию в настройках.', 'check authorization in Settings.', 'Anmeldung in den Einstellungen prüfen.', 'vérifiez l’autorisation dans les paramètres.', 'revisa la autorización en los ajustes.', 'controlla l’autorizzazione nelle impostazioni.', 'verifique a autorização nas definições.'),
    'forbidden': ('сервис отвечает, но доступ к API ограничен.', 'responding, but API access is restricted.', 'antwortet, aber der API-Zugriff ist eingeschränkt.', 'répond, mais l’accès à l’API est restreint.', 'responde, pero el acceso a la API está restringido.', 'risponde, ma l’accesso all’API è limitato.', 'responde, mas o acesso à API está limitado.'),
    'quota_exhausted': ('сервис отвечает, но доступная квота запросов исчерпана.', 'responding, but the request quota is exhausted.', 'antwortet, aber das Anfragekontingent ist aufgebraucht.', 'répond, mais le quota de requêtes est épuisé.', 'responde, pero se agotó la cuota de solicitudes.', 'risponde, ma la quota di richieste è esaurita.', 'responde, mas a quota de pedidos está esgotada.'),
    'rate_limited': ('сервис временно ограничил запросы.', 'requests are temporarily limited.', 'Anfragen sind vorübergehend begrenzt.', 'les requêtes sont temporairement limitées.', 'las solicitudes están limitadas temporalmente.', 'le richieste sono temporaneamente limitate.', 'os pedidos estão temporariamente limitados.'),
    'network': ('обнаружена высокая задержка или недоступность сети.', 'high network delay or a connection problem detected.', 'hohe Netzwerkverzögerung oder Verbindungsproblem erkannt.', 'délai réseau important ou problème de connexion détecté.', 'se detectó una demora de red elevada o un problema de conexión.', 'rilevato un ritardo di rete elevato o un problema di connessione.', 'detetado atraso elevado na rede ou problema de ligação.'),
    'unavailable': ('сейчас не удалось получить ответ сервиса.', 'no usable response received.', 'derzeit keine nutzbare Antwort erhalten.', 'aucune réponse exploitable pour le moment.', 'no se recibió una respuesta válida.', 'nessuna risposta utilizzabile al momento.', 'não foi recebida uma resposta válida.'),
}
_EMPTY = ('Проверка сервисов: нет включённых API-сервисов.', 'Service check: no API services enabled.', 'Dienstprüfung: keine API-Dienste aktiviert.', 'Vérification des services : aucune API activée.', 'Comprobación de servicios: no hay API activadas.', 'Verifica servizi: nessuna API abilitata.', 'Verificação de serviços: nenhuma API ativada.')


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    enabled: str
    secret: str = ''
    endpoint: str = ''
    auth: str = ''
    params: tuple = ()
    profile_index: int | None = None


SERVICES = (
    Service('searchapi', 'SearchAPI.io', 'gaming.web_knowledge.enabled'),
    Service('yandex_music', 'Yandex Music', 'integrations.yandex_music.enabled', 'integrations.yandex_music.token_encrypted', 'https://api.music.yandex.net/account/status', 'OAuth'),
    Service('telegram', 'Telegram', 'integrations.telegram.enabled', 'integrations.telegram.bot_token_encrypted', 'https://api.telegram.org/bot{token}/getMe', 'telegram'),
    Service('discord', 'Discord', 'integrations.discord.enabled', 'integrations.discord.bot_token_encrypted', 'https://discord.com/api/v10/users/@me', 'Bot'),
    Service('spotify', 'Spotify', 'integrations.spotify.enabled'),
    Service('gmail', 'Gmail', 'integrations.gmail.enabled'),
    Service('google_calendar', 'Google Calendar', 'integrations.google_calendar.enabled'),
    Service('openweather', 'OpenWeather', 'integrations.openweather.enabled', 'integrations.openweather.api_key_encrypted', 'https://api.openweathermap.org/data/2.5/weather', 'appid'),
    Service('youtube', 'YouTube', 'integrations.youtube.enabled', 'integrations.youtube.api_key_encrypted', 'https://www.googleapis.com/youtube/v3/videos', 'key', (('part', 'id'), ('id', 'jNQXAC9IVRw'))),
    Service('mailru', 'Mail.ru', 'integrations.mailru.enabled'),
)


def enabled_services(config):
    """Configuration-only discovery: never decrypt or connect on the UI thread."""
    out = [service for service in SERVICES if config.get(service.enabled, False)
           and (service.key != 'yandex_music' or config.get('integrations.yandex_music.api_enabled', True))]
    if config.get('integrations.discord.enabled', False):
        profiles = config.get('integrations.discord.servers', []) or []
        if not config.get('integrations.discord.bot_token_encrypted', '') and any(
                isinstance(p, dict) and p.get('enabled', True) and (p.get('bot_token_encrypted') or p.get('token_encrypted')) for p in profiles):
            out = [entry for entry in out if entry.key != 'discord']
        for index, profile in enumerate(config.get('integrations.discord.servers', []) or []):
            if isinstance(profile, dict) and profile.get('enabled', True):
                from dataclasses import replace
                base = next(x for x in SERVICES if x.key == 'discord')
                # Display ordinal only: server names are arbitrary user content.
                out.append(replace(base, key=f'discord.{index + 1}', name=f'Discord #{index + 1}', profile_index=index))
    return out


def format_status(key, status, code):
    index = _CODES.index(code) if code in _CODES else 1
    name = next((x.name for x in SERVICES if x.key == key), 'Discord #' + key.split('.')[-1] if key.startswith('discord.') else 'API')
    return name + ': ' + _STATUS.get(status, _STATUS['unavailable'])[index]


def empty_text(code):
    return _EMPTY[_CODES.index(code) if code in _CODES else 1]


def _result(service, status, *, http_status=None, error_type=None):
    # This is the complete allowlist for logs / ToolExecution.data.
    result = {'service': service.key, 'status': status, 'scope': 'read-only API response'}
    if isinstance(http_status, int):
        result['http_status'] = http_status
    if error_type:
        result['error_type'] = str(error_type)
    return result


def _failure(service, exc, http_status=None):
    import requests
    from jarvis.runtime.briefing_stream import section_failure
    status = getattr(getattr(exc, 'response', None), 'status_code', None) or http_status
    match = re.search(r'\bHTTP\s+(\d{3})\b', str(exc), re.IGNORECASE)
    if status is None and match:
        status = int(match[1])
    if getattr(exc, 'failure_kind', None) in {'quota_exhausted', 'rate_limited'}:
        kind = exc.failure_kind
    elif status == 401:
        kind = 'authentication'
    elif status == 403:
        kind = 'forbidden'
    elif status == 429:
        kind = 'rate_limited'
    elif isinstance(exc, (requests.Timeout, requests.ConnectionError, TimeoutError, ConnectionError, OSError)):
        kind = 'network'
    elif section_failure(exc)['reason'] == 'configuration':
        kind = section_failure(exc)['failure_kind']
    else:
        kind = 'unavailable'
    return _result(service, kind, http_status=status, error_type=type(exc).__name__)


def validation(value):
    """Reuse the briefing scheduler for independent retries; never sleep here."""
    payload = value[1]
    if not isinstance(payload, dict) or 'service' not in payload:
        return None
    status = payload.get('status')
    if status == 'ok':
        return None
    return {'error_type': payload.get('error_type') or 'ServiceUnavailable',
            'retryable': status in {'network', 'unavailable', 'rate_limited'},
            'reason': 'configuration' if status in {'configuration', 'authentication', 'forbidden'} else 'unavailable',
            'service_status': status, 'service_result': payload,
            'failure_kind': status, 'http_status': payload.get('http_status')}


def _session():
    import requests
    class BoundedSession(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs['timeout'] = (3.0, 5.0)
            kwargs['allow_redirects'] = False
            self.last_status = None
            response = super().request(method, url, **kwargs)
            self.last_status = int(response.status_code)
            return response
    return BoundedSession()


def _read_json(session, endpoint, *, headers=None, params=None):
    response = session.get(endpoint, headers=headers or {}, params=params or {}, timeout=(3.0, 5.0), allow_redirects=False)
    if not 200 <= int(response.status_code) < 300:
        error = RuntimeError(f'API HTTP {int(response.status_code)}')
        try:
            payload = response.json()
            reasons = (payload.get('error') or {}).get('errors') or []
            reason = reasons[0].get('reason') if reasons and isinstance(reasons[0], dict) else ''
            if reason in {'quotaExceeded', 'dailyLimitExceeded'}:
                error.failure_kind = 'quota_exhausted'
            elif reason in {'rateLimitExceeded', 'userRateLimitExceeded'}:
                error.failure_kind = 'rate_limited'
        except (ValueError, AttributeError, TypeError):
            pass
        raise error
    body = response.json()
    if not isinstance(body, dict) or body.get('error') or body.get('errors'):
        raise ValueError('Invalid API response')
    return body



def _validate_body(key, body):
    """Validate endpoint envelopes, discard all account and content fields."""
    if not isinstance(body, dict) or body.get('error') or body.get('errors'):
        raise ValueError('Invalid API response')
    valid = False
    if key == 'searchapi':
        account = body.get('account')
        valid = isinstance(account, dict) and any(isinstance(account.get(name), (int, float)) for name in ('remaining_credits', 'current_month_usage', 'monthly_allowance'))
    elif key == 'yandex_music':
        result = body.get('result')
        valid = isinstance(result, dict) and isinstance(result.get('account'), dict) and bool(result['account'].get('uid'))
    elif key == 'telegram':
        valid = body.get('ok') is True and isinstance(body.get('result'), dict) and bool(body['result'].get('id'))
    elif key.startswith('discord') or key == 'spotify':
        valid = bool(body.get('id'))
    elif key == 'gmail':
        valid = isinstance(body.get('emailAddress'), str) and bool(body['emailAddress'])
    elif key == 'google_calendar':
        valid = body.get('kind') == 'calendar#calendarList' and isinstance(body.get('items', []), list)
    elif key == 'openweather':
        valid = isinstance(body.get('main'), dict) and isinstance(body['main'].get('temp'), (float, int))
    elif key == 'youtube':
        valid = body.get('kind') == 'youtube#videoListResponse' and isinstance(body.get('items'), list)
    if not valid:
        raise ValueError('Invalid API response shape')
    if key == 'searchapi':
        credits = body['account'].get('remaining_credits')
        if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits <= 0:
            return 'quota_exhausted'
    return 'ok'


def probe(config, service):
    """One attempt. Requests are GET except existing OAuth refresh POSTs."""
    from jarvis.secrets import unprotect_secret
    session = None
    try:
        if service.key == 'mailru':
            import imaplib
            email = str(config.get('integrations.mailru.email', '') or '').strip()
            password = unprotect_secret(str(config.get('integrations.mailru.app_password_encrypted', '') or ''))
            if not email or not password:
                return _result(service, 'configuration')
            with imaplib.IMAP4_SSL('imap.mail.ru', 993, timeout=5.0) as mail:
                mail.login(email, password)
                # NOOP verifies the authenticated session without fetching mail.
                kind, _ = mail.noop()
                if kind != 'OK':
                    raise RuntimeError('IMAP unavailable')
            return _result(service, 'ok')
        if service.key in {'gmail', 'google_calendar', 'spotify'}:
            if service.key == 'gmail':
                from jarvis.integrations.mail_services import GmailClient
                client = GmailClient(config)
                client._session.close()
                session = client._session = _session()
                if not client.configured():
                    return _result(service, 'configuration')
                body = client.profile()
            elif service.key == 'google_calendar':
                from jarvis.integrations.google_calendar import GoogleCalendarClient
                client = GoogleCalendarClient(config)
                client._session.close()
                session = client._session = _session()
                if not client.configured():
                    return _result(service, 'configuration')
                body = client._request('GET', '/users/me/calendarList', params={'maxResults': 1})
            else:
                from jarvis.integrations.spotify import SpotifyAdapter
                client = SpotifyAdapter(config)
                client.session.close()
                session = client.session = _session()
                body = client._request('GET', '/me', allow_204=False)
            status = getattr(session, 'last_status', None)
            if status is not None and status != 200:
                raise RuntimeError(f'API HTTP {status}')
            return _result(service, _validate_body(service.key, body))
        session = _session()
        headers, params = {}, dict(service.params)
        endpoint = service.endpoint
        if service.key == 'searchapi':
            from jarvis.game_web.searchapi import SearchAPIGoogleProvider, SEARCHAPI_ACCOUNT_URL
            provider = SearchAPIGoogleProvider(config, session=session)
            secret = provider.api_key()
            endpoint = SEARCHAPI_ACCOUNT_URL
            params['api_key'] = secret
        else:
            protected = str(config.get(service.secret, '') or '')
            if service.profile_index is not None:
                profile = config.get('integrations.discord.servers', [])[service.profile_index]
                protected = str(profile.get('bot_token_encrypted') or profile.get('token_encrypted') or protected)
            secret = str(unprotect_secret(protected) or '').strip()
            if service.auth in {'OAuth', 'Bot'}:
                headers['Authorization'] = service.auth + ' ' + secret
            elif service.auth == 'telegram':
                if secret and not re.fullmatch(r'\d{5,14}:[A-Za-z0-9_-]{20,100}', secret):
                    return _result(service, 'configuration')
                endpoint = endpoint.format(token=secret)
            else:
                params[service.auth] = secret
            if service.key == 'openweather':
                # Check a known coordinate if no default city was configured.
                city = str(config.get('integrations.openweather.city', '') or '').strip()
                params.update({'q': city} if city else {'lat': 0, 'lon': 0})
        if not secret:
            return _result(service, 'configuration')
        body = _read_json(session, endpoint, headers=headers, params=params)
        return _result(service, _validate_body(service.key, body))
    except Exception as exc:
        return _failure(service, exc, getattr(session, 'last_status', None))
    finally:
        if session is not None:
            session.close()


def task(config, service, code):
    def read():
        result = probe(config, service)
        return format_status(service.key, result['status'], code), result
    return read
