"""Read-only service checks: actual clients, fake HTTP, real Qt briefing delivery."""
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from jarvis.integrations import service_checks161 as checks
from jarvis.runtime import briefing_stream as stream
from jarvis.secrets import protect_secret


class Config:
    def __init__(self, **values):
        self.values = values
    def get(self, key, default=None):
        return self.values.get(key, default)


class Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False
        self.headers = {}
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        self.last_status = None
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        status, body = response
        self.last_status = status
        return SimpleNamespace(status_code=status, content=b'fixture', headers={}, json=lambda: deepcopy(body))
    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)
    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)
    def close(self):
        self.closed = True


def service(key):
    return next(x for x in checks.SERVICES if x.key == key)


def secret(value='PRIVATE_TOKEN'):
    return protect_secret(value)


@pytest.fixture(autouse=True)
def token_cache_reset(monkeypatch):
    from jarvis.integrations import google_calendar, mail_services
    monkeypatch.setattr(google_calendar, '_TOKEN_CACHE', {})
    monkeypatch.setattr(mail_services, '_GMAIL_TOKEN_CACHE', {})
    monkeypatch.setattr(stream, 'RETRY_DELAYS', (.03, .05))


def test_discovery_is_enabled_only_covers_every_existing_api_and_server_profiles():
    data = {x.enabled: True for x in checks.SERVICES}
    data['integrations.discord.servers'] = [{'enabled': True}, {'enabled': False}, {}]
    found = checks.enabled_services(Config(**data))
    assert [x.key for x in found] == [x.key for x in checks.SERVICES] + ['discord.1', 'discord.3']
    assert len(checks.SERVICES) == 10
    assert checks.enabled_services(Config()) == []


@pytest.mark.parametrize('key', ['searchapi', 'yandex_music', 'telegram', 'discord', 'openweather', 'youtube'])
def test_simple_api_probes_are_one_get_no_control_or_message_requests(key, monkeypatch):
    entry = service(key)
    token = '123456:abcdefghijklmnopqrstuvwxyz' if key == 'telegram' else 'PRIVATE_TOKEN'
    config = Config(**{entry.secret: secret(token), 'gaming.web_knowledge.searchapi_api_key_encrypted': secret(token)})
    bodies = {'searchapi': {'account': {'remaining_credits': 100}},
              'yandex_music': {'result': {'account': {'uid': 'PRIVATE_ACCOUNT'}}},
              'telegram': {'ok': True, 'result': {'id': 123}}, 'discord': {'id': 'PRIVATE_ACCOUNT'},
              'openweather': {'main': {'temp': 20}, 'cod': 200},
              'youtube': {'kind': 'youtube#videoListResponse', 'items': []}}
    http = Http([(200, bodies[key])])
    monkeypatch.setattr(checks, '_session', lambda: http)
    result = checks.probe(config, entry)
    assert result == {'service': key, 'status': 'ok', 'scope': 'read-only API response'}
    assert http.closed and len(http.calls) == 1
    method, url, kwargs = http.calls[0]
    assert method == 'GET' and kwargs['allow_redirects'] is False
    assert kwargs['timeout'] == (3.0, 5.0)
    assert all(x not in url for x in ('play', 'pause', '/next', 'sendMessage', '/search'))
    if key == 'searchapi':
        assert url == 'https://www.searchapi.io/api/v1/me'
        assert 'q' not in kwargs['params'] and 'engine' not in kwargs['params']
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('status,expected,retryable', [(401,'authentication',False), (403,'forbidden',False), (429,'rate_limited',True), (503,'unavailable',True)])
def test_http_failures_classified_without_provider_payload(status, expected, retryable, monkeypatch):
    http = Http([(status, {'error': 'PRIVATE_TOKEN'})])
    monkeypatch.setattr(checks, '_session', lambda: http)
    entry = service('yandex_music')
    result = checks.probe(Config(**{entry.secret: secret()}), entry)
    assert result['status'] == expected and result['http_status'] == status
    validation = checks.validation(('failure', result))
    assert validation['retryable'] is retryable
    assert 'PRIVATE' not in json.dumps(validation) and http.closed


@pytest.mark.parametrize('failure', [requests.ReadTimeout('https://secret/?token=PRIVATE'), requests.ConnectionError('PRIVATE'), requests.exceptions.SSLError('PRIVATE')])
def test_network_failures_dont_become_invalid_token(failure, monkeypatch):
    http = Http([failure])
    monkeypatch.setattr(checks, '_session', lambda: http)
    entry = service('yandex_music')
    result = checks.probe(Config(**{entry.secret: secret()}), entry)
    assert result['status'] == 'network' and checks.validation(('', result))['retryable']
    assert 'PRIVATE' not in json.dumps(result)


def test_unconfigured_provider_and_malformed_telegram_never_contact_server(monkeypatch):
    http = Http([])
    monkeypatch.setattr(checks, '_session', lambda: http)
    assert checks.probe(Config(), service('youtube'))['status'] == 'configuration'
    assert checks.probe(Config(**{service('telegram').secret: secret('https://attacker/')}), service('telegram'))['status'] == 'configuration'
    assert http.calls == []


@pytest.mark.parametrize('code', checks._CODES)
def test_status_and_wait_text_localized_for_all_languages(code):
    for key in [x.key for x in checks.SERVICES] + ['discord.2']:
        assert not stream.status_text('service.' + key, code).startswith('service.')
        for status in checks._STATUS:
            result = checks.format_status(key, status, code)
            assert result and 'PRIVATE' not in result and ' ms' not in result
            if code != 'ru':
                assert not any('А' <= c <= 'я' or c in 'Ёё' for c in result)
    assert checks.empty_text(code)


@pytest.mark.parametrize('expired', [True, False])
def test_actual_gmail_profile_refreshes_expired_or_rejected_token_before_status(expired, monkeypatch):
    responses = [(200, {'access_token':'NEW_PRIVATE','expires_in':3600}), (200, {'emailAddress':'PRIVATE_ACCOUNT'})]
    if not expired:
        responses.insert(0, (401, {'error': {'message':'old token'}}))
    http = Http(responses)
    monkeypatch.setattr(checks, '_session', lambda: http)
    config = Config(**{'integrations.gmail.client_id_encrypted':secret('CLIENT'),
                       'integrations.gmail.refresh_token_encrypted':secret('REFRESH'),
                       'integrations.gmail.access_token_encrypted':secret('OLD_PRIVATE'),
                       'integrations.gmail.token_expires_at':0 if expired else time.time()+3600})
    result = checks.probe(config, service('gmail'))
    assert result['status'] == 'ok' and http.closed
    assert sum(method == 'POST' for method, *_ in http.calls) == 1
    assert http.calls[-1][1].endswith('/users/me/profile')
    assert http.calls[-1][2]['headers']['Authorization'] == 'Bearer NEW_PRIVATE'
    assert 'PRIVATE' not in json.dumps(result)
    assert not http.responses


@pytest.mark.parametrize('expired', [True, False])
def test_actual_calendar_refreshes_expired_or_rejected_cached_token(expired, monkeypatch):
    responses = [(200, {'access_token':'NEW_PRIVATE','expires_in':3600}), (200, {'kind':'calendar#calendarList','items':[]})]
    if not expired:
        responses.insert(0, (401, {'error': {'message':'old token'}}))
    http = Http(responses)
    monkeypatch.setattr(checks, '_session', lambda: http)
    config = Config(**{'integrations.google_calendar.enabled':True,
                       'integrations.gmail.client_id_encrypted':secret('CLIENT'),
                       'integrations.google_calendar.refresh_token_encrypted':secret('REFRESH'),
                       'integrations.google_calendar.access_token_encrypted':secret('OLD_PRIVATE'),
                       'integrations.google_calendar.token_expires_at':0 if expired else time.time()+3600})
    result = checks.probe(config, service('google_calendar'))
    assert result['status'] == 'ok' and http.closed and not http.responses
    assert sum(method == 'POST' for method, *_ in http.calls) == 1
    assert http.calls[-1][1].endswith('/users/me/calendarList')
    assert http.calls[-1][2]['headers']['Authorization'] == 'Bearer NEW_PRIVATE'


def test_actual_spotify_refresh_is_only_post_no_player_mutation(monkeypatch):
    http = Http([(401, {}), (200, {'access_token': 'NEW_PRIVATE', 'expires_in': 3600}), (200, {'id': 'PRIVATE_ACCOUNT'})])
    monkeypatch.setattr(checks, '_session', lambda: http)
    config = Config(**{'integrations.spotify.access_token_encrypted':secret('OLD_PRIVATE'),
                       'integrations.spotify.client_id_encrypted':secret('CLIENT'),
                       'integrations.spotify.refresh_token_encrypted':secret('REFRESH'),
                       'integrations.spotify.token_expires_at':time.time()+3600})
    result = checks.probe(config, service('spotify'))
    assert result['status'] == 'ok' and http.closed and not http.responses
    assert [(m,u.split('/')[-1]) for m,u,_ in http.calls] == [('GET','me'),('POST','token'),('GET','me')]


def test_mailru_authentication_noop_never_fetches_or_marks_messages(monkeypatch):
    import imaplib
    fake = Mock()
    fake.__enter__ = Mock(return_value=fake)
    fake.__exit__ = Mock(return_value=None)
    fake.noop.return_value = ('OK', [])
    connect = Mock(return_value=fake)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', connect)
    config = Config(**{'integrations.mailru.email':'fixture@example.invalid', 'integrations.mailru.app_password_encrypted':secret()})
    assert checks.probe(config, service('mailru'))['status'] == 'ok'
    connect.assert_called_once_with('imap.mail.ru', 993, timeout=5.0)
    fake.noop.assert_called_once_with()
    fake.fetch.assert_not_called()
    fake.select.assert_not_called()
    fake.store.assert_not_called()


def test_discord_profile_uses_own_token_without_exposing_profile_name(monkeypatch):
    config = Config(**{'integrations.discord.enabled':True,'integrations.discord.servers':[{'name':'PRIVATE_NAME','bot_token_encrypted':secret('PROFILE_TOKEN')}], 'integrations.discord.bot_token_encrypted':secret('ROOT_TOKEN')})
    entry = checks.enabled_services(config)[-1]
    http = Http([(200, {'id':'PRIVATE_ACCOUNT'})])
    monkeypatch.setattr(checks, '_session', lambda: http)
    result = checks.probe(config, entry)
    assert result['service'] == 'discord.1' and result['status'] == 'ok'
    assert http.calls[0][2]['headers']['Authorization'] == 'Bot PROFILE_TOKEN'
    assert 'PRIVATE_NAME' not in entry.name and 'PRIVATE' not in json.dumps(result)


def test_three_independent_attempts_then_terminal_network_status_and_safe_diagnostics():
    entry = service('yandex_music')
    calls = []
    def read():
        calls.append(time.monotonic())
        return ('failed', checks._result(entry, 'network', error_type='ReadTimeout'))
    notes = []
    rows = list(stream.collect({'service.yandex_music':read}, timeout=2, max_attempts=3, validate=stream.validate_section_result, on_attempt=lambda *x:notes.append(x)))
    assert len(calls) == 3 and calls[1]-calls[0]>=.025 and calls[2]-calls[1]>=.045
    assert [x[1] for x in rows] == ['retrying','retrying','error']
    assert rows[-1][2]['service_status'] == 'network'
    assert notes[-1][3]['failure_kind'] == 'network' and notes[-1][3]['retryable']


def test_auth_failure_diagnostic_safe_and_skips_unhelpful_retry():
    data = stream.section_failure(RuntimeError('Gmail OAuth refresh: HTTP 401 secret=PRIVATE https://secret/'))
    assert data['failure_kind'] == 'authentication' and data['http_status'] == 401
    assert data['retryable'] is False and 'PRIVATE' not in json.dumps(data) and 'https' not in json.dumps(data)


# Actual Qt settings and stream integration tests.
from tests.test_neo158_briefing import session, BriefingWindow, close_window, permit, qt_app, pump
from tests.test_neo158_settings_io import app, config
from jarvis.ui.settings_dialog import SettingsDialog
from jarvis.testing.isolation import external_io_blocked
from PySide6.QtCore import QTimer


@pytest.mark.parametrize('code', checks._CODES)
def test_actual_checkbox_persists_true_and_false_in_all_locales(app, config, code):
    config.data['app']['language'] = code
    with external_io_blocked():
        first = SettingsDialog(config)
        try:
            checkbox = first.briefing_include_services
            assert not checkbox.isChecked()
            assert checkbox.text() and checkbox.toolTip()
            if code != 'ru':
                assert 'Проверять' not in checkbox.text() and 'Проверяет' not in checkbox.toolTip()
            checkbox.setChecked(True)
            first._save(close=False)
            config.reload()
            assert config.get('briefing.include_services') is True
        finally:
            first.reject(); first.deleteLater(); app.processEvents()
        second = SettingsDialog(config)
        try:
            assert second.briefing_include_services.isChecked()
            second.briefing_include_services.setChecked(False)
            second._save(close=False)
            config.reload()
            assert config.get('briefing.include_services') is False
        finally:
            second.reject(); second.deleteLater(); app.processEvents()


def enable(session, *keys):
    session.config.data['briefing']['include_services'] = True
    for entry in checks.SERVICES:
        path = entry.enabled.split('.')
        node = session.config.data
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = entry.key in keys


def test_opt_out_never_discovers_or_probes_services(session, monkeypatch):
    session.config.data['briefing']['include_services'] = False
    unexpected = Mock(side_effect=AssertionError('unexpected probe'))
    monkeypatch.setattr(checks, 'enabled_services', unexpected)
    app, window = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        window._start_morning_briefing()
        pump(app, lambda: not window._briefing_active)
        unexpected.assert_not_called()
        assert len(window.chat.items) == 1 and 'service_checks' not in session.executions[-1].data
    finally:
        close_window(app, window)


def test_checks_stream_independently_in_same_real_bubble_and_qt_remains_live(session, monkeypatch):
    enable(session, 'yandex_music', 'searchapi')
    release = threading.Event()
    entered = threading.Event()
    worker_ids = []
    def probe(config, entry):
        worker_ids.append(threading.get_ident())
        if entry.key == 'yandex_music':
            entered.set(); release.wait(2)
        return checks._result(entry, 'ok')
    monkeypatch.setattr(checks, 'probe', probe)
    app, window = qt_app(), BriefingWindow(session)
    beats = []
    heartbeat = QTimer(window)
    heartbeat.timeout.connect(lambda:beats.append(True)); heartbeat.start(3)
    permit(session)
    try:
        window._start_morning_briefing()
        pump(app, lambda: entered.is_set() and 'SearchAPI.io: отвечает.' in window.chat.items[0].text and len(beats)>2)
        bubble = window._briefing_bubble158
        assert 'Система: исправна' in bubble.text and 'Получаю информацию: Yandex Music' in bubble.text
        release.set()
        pump(app, lambda: not window._briefing_active)
        assert window._briefing_bubble158 is bubble and len(window.chat.items)==1
        assert 'Yandex Music: отвечает.' in bubble.text and 'Получаю информацию' not in bubble.text
        assert set(session.executions[-1].data['service_checks']) == {'searchapi','yandex_music'}
        assert all(i != threading.get_ident() for i in worker_ids)
    finally:
        release.set(); heartbeat.stop(); close_window(app, window)


def test_service_network_failure_retries_three_times_without_leaking_measurements(session, monkeypatch, caplog):
    enable(session, 'yandex_music')
    probe = Mock(return_value=checks._result(service('yandex_music'),'network',error_type='ReadTimeout'))
    monkeypatch.setattr(checks, 'probe', probe)
    caplog.set_level('INFO', logger='jarvis.tools.integrations')
    app, window = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        window._start_morning_briefing()
        pump(app, lambda: not window._briefing_active)
        text = window.chat.items[0].text
        assert probe.call_count == 3 and text.count('Yandex Music: обнаружена высокая задержка или недоступность сети.') == 1
        assert ' ms' not in text and 'ReadTimeout' not in text
        assert 'failure_kind=network' in caplog.text
        data = session.executions[-1].data
        assert data['section_attempts']['service.yandex_music'] == 3 and data['partial']
    finally:
        close_window(app, window)


def test_cached_weather_cannot_falsely_prove_live_api_connection(session, monkeypatch):
    from jarvis.tools import integrations
    enable(session, 'openweather')
    session.config.data['briefing']['include_weather'] = True
    session.config.data['integrations']['openweather']['city'] = 'Fixture'
    weather = Mock(return_value=('Погода: ясно.', {'temp':20}))
    probe = Mock(return_value=checks._result(service('openweather'), 'network', error_type='ReadTimeout'))
    monkeypatch.setattr(integrations.OpenWeatherAdapter, 'answer', weather)
    monkeypatch.setattr(checks, 'probe', probe)
    app, window = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        window._start_morning_briefing()
        pump(app, lambda: not window._briefing_active)
        weather.assert_called_once(); assert probe.call_count == 3
        text = window.chat.items[0].text
        assert 'Погода: ясно.' in text and 'OpenWeather: обнаружена высокая задержка' in text
        assert 'OpenWeather: отвечает.' not in text
        assert session.executions[-1].data['service_checks']['openweather']['scope'] == 'read-only API response'
    finally:
        close_window(app, window)


def test_enabled_empty_services_show_finite_clear_result(session, monkeypatch):
    enable(session)
    app, window = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        window._start_morning_briefing()
        pump(app, lambda: not window._briefing_active)
        assert 'Проверка сервисов: нет включённых API-сервисов.' in window.chat.items[0].text
        assert session.executions[-1].data['service_checks'] == {}
    finally:
        close_window(app, window)


def test_api_disabled_yandex_is_not_probed_even_if_browser_music_enabled():
    config = Config(**{'integrations.yandex_music.enabled':True, 'integrations.yandex_music.api_enabled':False})
    assert checks.enabled_services(config) == []


def test_telegram_settings_same_boolean_and_native_labels():
    from jarvis.integrations.telegram_settings import SETTINGS
    from jarvis.integrations.telegram_setting_labels import LABELS
    setting = SETTINGS['briefing_include_services']
    assert setting.title in LABELS and len(LABELS[setting.title]) == 6
    assert setting.path == 'briefing.include_services' and setting.default is False
    assert setting.parse('1') is True and setting.parse('0') is False
    for code in checks._CODES:
        assert setting.label(code)
        if code != 'ru':
            assert 'Проверять' not in setting.label(code)


def test_changed_briefing_toggle_cannot_overlap_hung_api_read():
    release, entered = threading.Event(), threading.Event()
    old_calls, new_calls = [], []
    scope = object()
    def old():
        old_calls.append(True); entered.set(); release.wait(2)
        return ('old', {})
    def new():
        new_calls.append(True)
        return ('new', {})
    first = list(stream.collect({'service.gmail': old}, timeout=.08, scope=scope))
    assert entered.is_set() and first[-1][1] == 'timeout'
    try:
        second = list(stream.collect({'mail.gmail':new}, timeout=.08, scope=scope))
        assert second[-1][1] == 'timeout' and not new_calls
    finally:
        release.set()
    deadline = time.monotonic()+2
    while (scope,'mail.gmail') in stream._ACTIVE_READS and time.monotonic()<deadline:
        time.sleep(.001)
    third = list(stream.collect({'mail.gmail':new}, timeout=1, scope=scope))
    assert third == [('mail.gmail','ready',('new',{}))] and new_calls == [True]


def test_mailru_complete_failure_preserves_error_type_but_logs_no_exception_text(monkeypatch, caplog):
    from jarvis.integrations.mail_services import MailRuClient
    client = object.__new__(MailRuClient)
    client.account_email = 'PRIVATE_ACCOUNT'
    monkeypatch.setattr(client, 'selected_folders', lambda:['PRIVATE_FOLDER'])
    monkeypatch.setattr(client, '_folder_status', Mock(side_effect=TimeoutError('token=PRIVATE_TOKEN')))
    with pytest.raises(TimeoutError):
        client.status(include_latest=0)
    assert 'failure_kind=network' in caplog.text and 'PRIVATE' not in caplog.text


@pytest.mark.parametrize('key', [x.key for x in checks.SERVICES if x.key != 'mailru'])
@pytest.mark.parametrize('body', [{}, {'error':'PRIVATE'}, {'ok':True}])
def test_malformed_success_json_never_proves_service_health(key, body):
    with pytest.raises(ValueError):
        checks._validate_body(key, body)


def test_empty_real_resource_lists_are_valid_not_a_service_failure():
    assert checks._validate_body('google_calendar', {'kind':'calendar#calendarList'}) == 'ok'
    assert checks._validate_body('youtube', {'kind':'youtube#videoListResponse','items':[]}) == 'ok'


@pytest.mark.parametrize('status,expected,retryable', [(401,'authentication',False),(403,'forbidden',False),(429,'rate_limited',True)])
def test_real_spotify_exception_preserves_actual_http_classification(status, expected, retryable, monkeypatch):
    config = Config(**{'integrations.spotify.access_token_encrypted':secret('PRIVATE_ACCESS'),
                       'integrations.spotify.client_id_encrypted':secret('PRIVATE_CLIENT'),
                       'integrations.spotify.refresh_token_encrypted':secret('PRIVATE_REFRESH'),
                       'integrations.spotify.token_expires_at':time.time()+3600})
    responses = [(status, {'error':{'message':'PRIVATE_PROVIDER_DETAIL'}})]
    if status == 401:
        responses += [(200, {'access_token':'PRIVATE_NEW','expires_in':3600}), (401, {'error':{'message':'PRIVATE_PROVIDER_DETAIL'}})]
    http = Http(responses)
    monkeypatch.setattr(checks, '_session', lambda:http)
    result = checks.probe(config, service('spotify'))
    assert result['status'] == expected and result['http_status'] == status
    assert checks.validation(('',result))['retryable'] is retryable
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('key,body', [('searchapi',{'account':{'remaining_credits':0}}), ('youtube',{'error':{'errors':[{'reason':'quotaExceeded'}]}})])
def test_quota_exhausted_is_distinct_from_network_and_not_retried(key, body, monkeypatch):
    entry = service(key)
    config = Config(**{entry.secret:secret(), 'gaming.web_knowledge.searchapi_api_key_encrypted':secret()})
    http = Http([(200 if key=='searchapi' else 403,body)])
    monkeypatch.setattr(checks, '_session', lambda:http)
    result = checks.probe(config, entry)
    assert result['status']=='quota_exhausted'
    assert not checks.validation(('',result))['retryable']


def test_discord_only_server_tokens_dont_show_unused_global_profile_error():
    config = Config(**{'integrations.discord.enabled':True, 'integrations.discord.bot_token_encrypted':'',
                       'integrations.discord.servers':[{'enabled':True,'bot_token_encrypted':secret()}]})
    assert [x.key for x in checks.enabled_services(config)] == ['discord.1']


def test_default_empty_mail_summary_is_not_network_health_evidence(session, monkeypatch):
    from jarvis.integrations import mail_services
    from jarvis.tools import integrations
    enable(session, 'gmail')
    session.config.data['integrations']['gmail']['include_in_briefing']=True
    status = Mock(return_value=SimpleNamespace(compact=lambda:{'total':0,'provider':'gmail'}))
    monkeypatch.setattr(mail_services,'provider_client',lambda *_:SimpleNamespace(configured=lambda:True,status=status))
    monkeypatch.setattr(integrations,'format_mail_briefing',lambda *_:'Gmail: нет выбранных папок.')
    probe = Mock(return_value=checks._result(service('gmail'),'authentication',http_status=401))
    monkeypatch.setattr(checks,'probe',probe)
    app,window=qt_app(),BriefingWindow(session)
    permit(session)
    try:
        window._start_morning_briefing()
        pump(app,lambda:not window._briefing_active)
        status.assert_called_once();probe.assert_called_once()
        text=window.chat.items[0].text
        assert 'Gmail: нет выбранных папок.' in text
        assert 'Gmail: нужно проверить авторизацию' in text
        assert 'Gmail: отвечает.' not in text
    finally:
        close_window(app,window)


@pytest.mark.parametrize('key,body', [('gmail', {'emailAddress':'PRIVATE_ACCOUNT'}), ('google_calendar', {'kind':'calendar#calendarList','items':[]}), ('spotify', {'id':'PRIVATE_ACCOUNT'})])
@pytest.mark.parametrize('http_code', [204, 302])
def test_oauth_non_ok_http_cannot_pass_even_with_success_shaped_body(key, body, http_code, monkeypatch):
    config = Config(**{
        'integrations.google_calendar.enabled':True,
        'integrations.gmail.client_id_encrypted':secret('PRIVATE_CLIENT'),
        'integrations.gmail.refresh_token_encrypted':secret('PRIVATE_REFRESH'),
        'integrations.gmail.access_token_encrypted':secret('PRIVATE_ACCESS'),
        'integrations.gmail.token_expires_at':time.time()+3600,
        'integrations.google_calendar.refresh_token_encrypted':secret('PRIVATE_REFRESH'),
        'integrations.google_calendar.access_token_encrypted':secret('PRIVATE_ACCESS'),
        'integrations.google_calendar.token_expires_at':time.time()+3600,
        'integrations.spotify.access_token_encrypted':secret('PRIVATE_ACCESS'),
        'integrations.spotify.token_expires_at':time.time()+3600})
    http = Http([(http_code,body)])
    monkeypatch.setattr(checks,'_session',lambda:http)
    result=checks.probe(config,service(key))
    assert result['status']=='unavailable' and result['http_status']==http_code
    assert 'PRIVATE' not in json.dumps(result) and http.closed
