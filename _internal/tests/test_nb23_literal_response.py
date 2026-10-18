"""Scenario result framing must not translate names or infer terminal state from run."""
from copy import deepcopy

import pytest

from jarvis.runtime.localized_text import localize_runtime_text, render_action_result, _FALLBACK
from jarvis.ui.scenario_strings import st

LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


@pytest.mark.parametrize('code', LANGS)
@pytest.mark.parametrize('status', ('completed', 'failed', 'partial', 'cancelled'))
def test_scenario_terminal_state_and_literal_details(code, status):
    name = 'Ожидание <b>Иван & Chloé</b>'
    data = {
        'name': name, 'status': status,
        'summary': {'completed': 1, 'failed': 1 if status == 'failed' else 0,
                    'skipped': 0, 'not_run': 0 if status == 'completed' else 1},
        'outcomes': [{'step_index': 1, 'success': status == 'completed',
                      'cancelled': status == 'cancelled', 'opened': status == 'failed',
                      'path': r'C:\Работа\résumé.txt',
                      'error': 'monitor 2 unavailable' if status == 'failed' else ''},
                     {'step_index': 2, 'success': False, 'not_run': True}],
        'access_token': 'do-not-display', 'password': 'also-private',
    }
    before = deepcopy(data)
    text = render_action_result('scenario', {'action': 'run'}, status == 'completed',
                                'Started', data, code)
    assert text.startswith(name + ': ' + st(status, code))
    assert st('result_summary', code, **data['summary']) in text
    assert r'C:\Работа\résumé.txt' in text
    assert st('result_not_run', code) in text
    if status == 'failed':
        assert st('result_opened', code) in text
        assert st('monitor_unavailable', code, number='2') in text
    assert 'do-not-display' not in text and 'also-private' not in text
    assert data == before


@pytest.mark.parametrize('code', LANGS)
def test_scenario_retry_projection_keeps_provider_diagnostic(code):
    data = {'name': 'Работа', 'status': 'failed', 'error': 'Путь erreur <file> 404',
            'outcomes': [], 'summary': {}}
    assert render_action_result('scenario', {'action': 'retry'}, False, '', data, code) == (
        'Работа: ' + st('failed', code) + '\nПуть erreur <file> 404')


@pytest.mark.parametrize('code', LANGS[1:])
def test_legacy_technical_localization_still_runs(code):
    assert localize_runtime_text('Ошибка: ECONNRESET 5', code).endswith('ECONNRESET 5')
    assert localize_runtime_text('Ошибка: ECONNRESET 5', code) != 'Ошибка: ECONNRESET 5'
    opened = localize_runtime_text('Открыл приложение: Редактор', code)
    assert 'Редактор' in opened and not opened.startswith('Открыл')
    assert localize_runtime_text('Незнакомый внутренний ответ', code) == _FALLBACK[code][1]
