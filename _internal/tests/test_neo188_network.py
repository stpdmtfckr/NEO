"""NEO 188 NEO-NET-001 / TZ-NETWORK-LOAD-LATENCY-CONNECTIVITY-01."""
import socket

import pytest

from jarvis.runtime.desktop_services import NetworkState, event_text

IF = (('ethernet', ('10.0.0.2',)),)


def feed(samples, threshold=250, context=None):
    state, keys = NetworkState(), []
    for sample in samples:
        ctx = context(sample) if callable(context) else (context or {})
        value = sample if not isinstance(sample, tuple) else sample[0]
        for event in state.observe(IF, set(), value, threshold, ctx):
            if event['type'] == 'notification':
                keys.append((event['message_key'], event['data']))
    return state, keys


def test_net01_heavy_download_with_normal_latency_is_silent():
    _, keys = feed([40, 60, 80, 90, 70, 60] * 3, context={'downloading': True})
    assert keys == []


def test_net02_saturated_link_reports_latency_not_outage():
    _, keys = feed([40, 600, 700, 800, 650], context={'downloading': True})
    assert [k for k, _ in keys] == ['network.latency_high']
    text = event_text({'message_key': 'network.latency_high', 'data': keys[0][1]}, 'ru')
    assert 'пока идёт загрузка' in text and 'Подключение к интернету есть' in text and 'пропало' not in text


def test_net03_real_outage_during_install_is_reported_once():
    _, keys = feed([40, 900, -1, -1, -1, -1, 40, 40, 40], context={'downloading': True})
    assert [k for k, _ in keys] == ['network.offline', 'network.restored']


def test_net04_one_endpoint_down_is_not_a_global_outage():
    _, keys = feed([40, 45, 50, 55, 60], context={'endpoint_failed': True})
    assert keys == []


def test_net05_debounce_and_threshold_borders():
    _, keys = feed([40, 900, 40, 900, 40, 900, 40])           # alternating: no storm
    assert keys == []
    _, keys = feed([250, 250, 250, 250])                      # exactly the threshold is not «slow»
    assert keys == []
    _, keys = feed([251, 251, 251, 175, 175])                 # 0.7 × 250 = 175 is not yet «low»
    assert [k for k, _ in keys] == ['network.latency_high']
    _, keys = feed([251, 251, 251, 174, 174])
    assert [k for k, _ in keys] == ['network.latency_high', 'network.latency_normal']


def test_net06_recovery_texts_follow_the_state_that_changed():
    _, keys = feed([40, 600, 600, 600, 40, 40, -1, -1, -1, 40, 40])
    assert [k for k, _ in keys] == ['network.latency_high', 'network.latency_normal', 'network.offline', 'network.restored']
    texts = {k: event_text({'message_key': k, 'data': d}, 'ru') for k, d in keys}
    assert texts['network.latency_normal'] == 'Задержка сети снова в норме.'
    assert texts['network.restored'] == 'Подключение к интернету восстановлено.'


def test_net07_user_threshold_is_applied():
    _, keys = feed([400, 400, 400], threshold=500)
    assert keys == []
    _, keys = feed([400, 400, 400], threshold=300)
    assert [k for k, _ in keys] == ['network.latency_high']


@pytest.mark.parametrize('code', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_net09_texts_in_seven_languages(code):
    keys = ('network.latency_high', 'network.latency_normal', 'network.offline', 'network.restored')
    texts = [event_text({'message_key': key, 'data': {}}, code) for key in keys]
    assert len(set(texts)) == 4 and all(texts) and not any(ch.isdigit() for t in texts for ch in t)


def test_probe_uses_independent_endpoints_only_after_a_failure(monkeypatch):
    from jarvis.runtime import desktop_services as ds
    targets = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def connect(target, timeout):
        targets.append(target)
        if target[0] == 'www.searchapi.io':
            raise OSError('unreachable')
        return Conn()

    monkeypatch.setattr(socket, 'create_connection', connect)
    service = ds.DesktopServices.__new__(ds.DesktopServices)
    service.config = type('C', (), {'get': lambda self, key, default=None: {'security.latency_notifications_enabled': True,
                                                                            'security.latency_threshold_ms': 250}.get(key, default)})()
    seen = []
    service.network = type('N', (), {'observe': lambda self, *a: seen.append(a) or []})()
    service.store = None
    service.callback = None
    service._network_probe()
    assert targets[:2] == [('www.searchapi.io', 443), ('1.1.1.1', 443)] and len(targets) == 2
    assert seen[0][2] >= 0 and seen[0][4]['endpoint_failed'] is True
