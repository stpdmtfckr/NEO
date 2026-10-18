"""A network flap produces bounded, plain-language notices, not measurements."""
import pytest

from jarvis.runtime.desktop_services import NetworkState, event_text


@pytest.mark.parametrize('language', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_network_outage_and_recovery_are_announced_once(language):
    state = NetworkState()
    events = []
    for latency in [20, 20, -1, -1, -1, -1, -1, 900, 800, 20, 20, 20]:
        events.extend(state.observe(('ethernet',), set(), latency, 250))
    notices = [item for item in events if item['type'] == 'notification']
    # NEO 188 NEO-NET-001: five failed probes are an outage, not «high latency».
    assert [item['message_key'] for item in notices] == [
        'network.offline', 'network.restored',
    ]
    for item in notices:
        text = event_text(item, language)
        assert text and len(text) < 100
        assert not any(char.isdigit() for char in text)
        assert 'latency_ms' not in text
    if language == 'ru':
        assert event_text(notices[0], language) == 'Похоже, подключение к интернету пропало: проверочные адреса не отвечают.'


def test_one_transient_sample_does_not_interrupt_music_clients():
    state = NetworkState()
    state.observe(('ethernet',), set(), 20, 250)
    assert state.observe(('ethernet',), set(), -1, 250) == []
    assert state.observe(('ethernet',), set(), 20, 250) == []
