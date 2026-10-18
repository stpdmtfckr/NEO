"""NEO 172: «не то» teaches a correction; phrases are collected only with consent; the weekly report."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.runtime import learning172


def _config(tmp_path, **values):
    data = {'learning.collect_phrases': True, 'memory.enabled': True, **values}
    return SimpleNamespace(get=lambda k, d=None: data.get(k, d), paths=SimpleNamespace(root=tmp_path), data=data)


def test_nothing_is_collected_without_consent(tmp_path):
    config = _config(tmp_path, **{'learning.collect_phrases': False})
    assert not learning172.record(config, 'открой браузер', 'ru', 'browser', 'open')
    assert learning172.phrases(config) == []
    assert learning172.report_text(config, 'ru').startswith('Сбор фраз для проверки выключен.')


def test_only_short_commands_are_kept_deduplicated_and_limited(tmp_path, monkeypatch):
    config = _config(tmp_path)
    assert learning172.record(config, 'Открой  браузер', 'ru', 'browser', 'open')
    assert learning172.record(config, 'открой браузер!', 'ru', 'browser', 'open')         # same phrase: replaced
    assert not learning172.record(config, 'слово ' * 25, 'ru', 'ui', 'type')              # dictation is not a command
    rows = learning172.phrases(config)
    assert len(rows) == 1 and rows[0]['intent'] == 'browser.open' and rows[0]['text'] == 'открой браузер!'
    assert set(rows[0]) == {'time', 'language', 'text', 'intent', 'source'}                 # nothing else is stored
    monkeypatch.setattr(learning172, 'LIMIT', 5)
    for i in range(9):
        learning172.record(config, f'команда {i}', 'ru', 'app', 'open')
    assert len(learning172.phrases(config)) == 5
    assert learning172.clear(config) == 5 and learning172.phrases(config) == []


def test_report_measures_and_compares_with_last_time(tmp_path):
    config = _config(tmp_path)
    for i in range(10):
        learning172.record(config, f'открой программу {i}', 'ru', 'application', 'open')
    assert learning172.report_text(config, 'ru', predict=lambda t, c: 'application.open' if not t.endswith(('8', '9')) else 'x') \
        .startswith('Как я вас понимаю: 80% команд из 10 собранных фраз узнаю правильно.')
    text = learning172.report_text(config, 'ru', predict=lambda t, c: 'application.open' if not t.endswith('9') else 'x')
    assert text.startswith('Как я вас понимаю: 90% команд из 10 собранных фраз узнаю правильно (было 80%).')
    assert learning172.report_text(_config(tmp_path / 'b'), 'ru').startswith('Пока собрано 0 фраз')


def test_weekly_timing(tmp_path):
    config = _config(tmp_path)
    for i in range(10):
        learning172.record(config, f'открой {i}', 'ru', 'application', 'open')
    assert learning172.weekly_due(config)
    learning172.measure(config, predict=lambda t, c: 'application.open')
    assert not learning172.weekly_due(config)
    assert learning172.weekly_due(config, now=datetime.now(timezone.utc) + timedelta(days=8))


def test_real_command_model_is_used_by_default(tmp_path):
    config = _config(tmp_path)
    root = Path(__file__).resolve().parents[1]
    config.paths.root = tmp_path
    from jarvis.neo_model import runtime
    model = runtime.model(SimpleNamespace(get=lambda k, d=None: d, paths=SimpleNamespace(root=root / '_no_user_data_')))
    if model is None:
        pytest.skip('no packaged command model in this tree')


def _session(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    session = AtlasSession(tmp_path / 'run')
    session.reset_case('ru')
    return session


def test_not_that_then_the_right_command_becomes_a_rule(tmp_path):
    from jarvis.runtime import memory169
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = _session(tmp_path)
        try:
            session.agent.config.data.setdefault('learning', {})['collect_phrases'] = True
            session.agent.handle('включи музыку')
            first = session.calls[-1]
            asked = session.agent.handle('не то')
            assert asked.text.startswith('Что вы имели в виду? Скажите команду так, как нужно') and '«включи музыку»' in asked.text
            session.calls.clear()
            session.agent.handle('открой браузер')
            right = session.calls[0]
            assert right['tool'] != 'music'
            rule = memory169.correction_rule(session.agent.config, 'включи музыку', 'ru')
            assert rule is not None and (rule[0], rule[1].get('action')) == (right['tool'], right['arguments'].get('action'))
            assert first['tool'] == 'music'
            collected = {(r['text'], r['intent'], r['source']) for r in learning172.phrases(session.agent.config)}
            assert ('включи музыку', f"{right['tool']}.{right['arguments']['action']}", 'correction') in collected
            assert ('включи музыку', 'music.play_music', 'executed') not in collected      # the correction replaced it
            # the rule works at once: the same phrase now means the corrected command
            session.calls.clear()
            session.agent.handle('включи музыку')
            assert session.calls[0]['tool'] == right['tool']
        finally:
            memory169_path = memory169.corrections_path(session.agent.config)
            if memory169_path and memory169_path.exists():
                memory169_path.unlink()
            session.finish()


def test_not_that_without_a_previous_command(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = _session(tmp_path)
        try:
            session.agent._last_command169 = None
            assert session.agent.handle('не то').text.startswith('Не помню предыдущей команды')
        finally:
            session.finish()


@pytest.mark.parametrize('text', ['как ты меня понимаешь', 'отчёт об обучении', 'how well do you understand me', 'wie gut verstehst du mich',
                                  'quanto mi capisci', 'relatório de aprendizagem'])
def test_report_phrases(text):
    from jarvis.runtime.everyday172 import _REPORT, _n
    assert _REPORT.match(_n(text))
