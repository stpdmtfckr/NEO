"""NEO 167: match-found notices, Telegram «Accept» button, (auto) accepting."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.gaming import match_ready as mr
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Telegram:
    def __init__(self):
        self.sent = []

    def notify_with_buttons(self, text, rows):
        self.sent.append((text, rows))


def watcher(language='ru', **values):
    data = {'app.language': language, **values}
    config = SimpleNamespace(get=lambda key, default=None: data.get(key, default))
    clock = Clock()
    w = mr.MatchReadyWatcher(config, is_windows=True, clock=clock)
    notices, telegram, clicks, league = [], Telegram(), [], []
    w.set_notice_callback(notices.append)
    w.attach_telegram(telegram)
    w.window_accept = lambda event: clicks.append(event.game) or True
    w.league_accept = lambda: league.append(1) or False
    w._league_lockfile = lambda: None
    return w, clock, notices, telegram, clicks


def dota(**kw):
    return mr.MatchEvent(game='Dota 2', process='dota2.exe', hwnd=42, **kw)


@pytest.mark.parametrize('code', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_every_message_exists_in_every_language(code):
    for key in mr._TEXT:
        assert mr.message(key, code, game='G', minutes=5, ago='1 min')


def test_found_match_is_announced_once_with_an_accept_button():
    w, clock, notices, telegram, clicks = watcher()
    w.on_match_found(dota(at=clock.now), certain=False)
    w.on_match_found(dota(at=clock.now), certain=False)   # the taskbar keeps flashing
    assert len(notices) == 1 and 'Dota 2' in notices[0] and 'прими матч' in notices[0]
    assert telegram.sent == [(notices[0], [[('✅ Принять', 'a:match:accept')]])]
    assert clicks == []                                    # never accepted on its own


def test_accept_after_notice_clicks_the_button_of_that_game():
    w, clock, notices, telegram, clicks = watcher('en')
    w.on_match_found(dota(at=clock.now), certain=False)
    clock.now += 5
    ok, text = w.accept()
    assert ok and clicks == ['Dota 2'] and text == '✅ Dota 2: match accepted.'
    clock.now += 1
    assert w.accept() == (False, 'There is no found match waiting to be accepted.')


def test_old_notice_is_not_accepted():
    w, clock, _, _, clicks = watcher()
    w.on_match_found(dota(at=clock.now), certain=False)
    clock.now += 120
    w._foreground_game_event = lambda **kw: None
    ok, text = w.accept()
    assert not ok and clicks == [] and 'нет найденного матча' in text


def test_armed_mode_accepts_the_next_match_once_and_reports_it():
    w, clock, notices, telegram, clicks = watcher()
    assert 'приму матч автоматически' in w.arm(30) and w.armed()
    w.on_match_found(dota(at=clock.now), certain=False)
    assert clicks == ['Dota 2'] and 'матч принят' in notices[0]
    assert telegram.sent[0][1] == []                      # nothing left to press
    assert not w.armed()                                  # one match only by default


def test_arming_right_after_a_notice_accepts_it_immediately():
    w, clock, notices, _, clicks = watcher()
    w.on_match_found(dota(at=clock.now), certain=False)
    clock.now += 3
    text = w.arm()
    assert clicks == ['Dota 2'] and 'матч принят' in text


def test_missing_button_is_reported_and_nothing_else_happens():
    w, clock, _, _, clicks = watcher()
    w.window_accept = lambda event: False
    w.on_match_found(dota(at=clock.now), certain=False)
    ok, text = w.accept()
    assert not ok and 'не нашёл кнопку' in text


def test_league_uses_its_client_api():
    w, clock, notices, _, clicks = watcher('en')
    w.league_accept = lambda: True
    w.on_match_found(mr.MatchEvent(game='League of Legends', at=clock.now), certain=True)
    assert notices[0].startswith('🎮 League of Legends: match found!')
    assert w.accept() == (True, '✅ League of Legends: match accepted.') and clicks == []


def test_status_and_disarm():
    w, clock, _, _, _ = watcher('en')
    assert w.status() == 'Auto-accept is off.'
    w.arm(10)
    assert w.status().startswith('Auto-accept is on for 10 more min.')
    assert w.disarm() == 'Match auto-accept is off.' and not w.armed()


def test_disabled_watcher_never_starts():
    w, *_ = watcher(**{'gaming.match_ready.enabled': False})
    assert not w.start()


def test_accept_word_detection_prefers_the_big_button():
    words = [('Accept', 10, 10, 20, 8), ('ПРИНЯТЬ', 500, 400, 200, 60), ('Decline', 0, 0, 50, 50)]
    assert mr.find_accept_word(words) == (600.0, 430.0)
    assert mr.find_accept_word([('Acceptance', 0, 0, 10, 10), ('Play', 0, 0, 10, 10)]) is None


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('match167') / 'run')
        yield value
        value.finish()


def _phrases():
    canonical = json.loads((ROOT / 'config/canonical_phrases/match.json').read_text(encoding='utf-8'))
    variants = json.loads((ROOT / 'config/command_variants/match.json').read_text(encoding='utf-8'))
    rows = [(LANGUAGES[i], phrases[i], intent) for intent, phrases in canonical.items() for i in range(7)]
    rows += [(code, phrase, intent) for intent, langs in variants.items() for code, phrases in langs.items() for phrase in phrases]
    return rows


def test_many_phrases_per_language():
    rows = _phrases()
    for code in LANGUAGES:
        for intent in ('match.accept', 'match.arm', 'match.disarm'):
            assert sum(1 for c, _, i in rows if c == code and i == intent) >= 4, (code, intent)


@pytest.mark.parametrize('code,phrase,intent', _phrases())
def test_every_match_phrase_routes_to_its_function(session, code, phrase, intent):
    session.reset_case(code)
    session.agent.handle(phrase)
    call = session.calls[0] if session.calls else None
    got = (call['tool'] + '.' + call['arguments'].get('action', '')) if call else ''
    assert got == intent, (code, phrase, session.calls)
