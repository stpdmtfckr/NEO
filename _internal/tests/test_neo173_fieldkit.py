"""NEO 173: the developer report holds no personal content; the quiet check after start."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.runtime import fieldkit173 as kit
from jarvis.runtime import http_pool173


def _config(tmp_path, **values):
    data = {'app.language': 'ru', 'llm_agent.enabled': True, 'local_chat.enabled': True, 'gaming.web_knowledge.enabled': True,
            'gaming.web_knowledge.searchapi_api_key_encrypted': 'dpapi:SECRETKEY123', 'integrations.gmail.enabled': True,
            'integrations.gmail.refresh_token_encrypted': 'dpapi:REFRESHTOKEN', 'integrations.gmail.account_email': 'vova@mail.ru', **values}
    return SimpleNamespace(get=lambda k, d=None: data.get(k, d), paths=SimpleNamespace(root=tmp_path, logs=tmp_path / 'logs'), data=data)


LOG = '''2026-09-30 10:00:01 INFO jarvis.agent Agent route=web user text 'мой пароль 12345'
2026-09-30 10:00:02 ERROR jarvis.integrations.mail_services Gmail failed for vova@mail.ru reading "Письмо от банка"
Traceback (most recent call last):
  File "C:\\\\Users\\\\Vova\\\\NEO\\\\jarvis\\\\integrations\\\\mail_services.py", line 10, in x
ConnectionError: Max retries exceeded with url: https://gmail.googleapis.com/users/vova
2026-09-30 10:00:03 ERROR jarvis.tools.registry boom
'''


def test_report_has_versions_setup_and_timings_but_no_personal_content(tmp_path):
    http_pool173.record('searchapi', 'google', 812.0)
    path, text = kit.report(_config(tmp_path), version='NEO v4.0.20.173', log_text=LOG,
                            ollama_state=lambda: {'installed': ['neo-agent:latest'], 'loaded': []},
                            self_test=lambda: {'checks': [{'name': 'ollama', 'status': 'PASS', 'ok': True, 'detail': 'C:\\Users\\Vova'}]},
                            now=datetime(2026, 9, 30, 12, 0, 0))
    assert path == tmp_path / 'reports' / 'neo_report_20260930_120000.txt' and path.read_text(encoding='utf-8') == text
    data = json.loads(text)
    assert data['neo_version'] == 'NEO v4.0.20.173' and data['setup']['gmail'] is True and data['setup']['searchapi_key'] is True
    assert data['service_timings_ms']['searchapi.google']['count'] >= 1
    assert data['self_test'] == [{'name': 'ollama', 'status': 'PASS', 'ok': True}]
    assert data['error_kinds'] == ['jarvis.integrations.mail_services: ConnectionError', 'jarvis.tools.registry']
    for secret in ('SECRETKEY123', 'REFRESHTOKEN', 'vova@mail.ru', 'Vova', 'пароль', 'Письмо от банка', '12345'):
        assert secret not in text, secret


def test_startup_problems_and_their_fixes(tmp_path):
    ok = lambda: True
    assert kit.startup_problems(_config(tmp_path), ollama_ok=ok, agent_model_ok=ok, ocr_ok=ok) == []
    assert kit.startup_problems(_config(tmp_path), ollama_ok=lambda: False, agent_model_ok=ok, ocr_ok=ok) == ['ollama']
    assert kit.startup_problems(_config(tmp_path), ollama_ok=ok, agent_model_ok=lambda: False, ocr_ok=ok) == ['agent_model']
    no_key = _config(tmp_path, **{'gaming.web_knowledge.searchapi_api_key_encrypted': ''})
    assert kit.startup_problems(no_key, ollama_ok=ok, agent_model_ok=ok, ocr_ok=ok) == ['searchapi_key']
    text = kit.problems_text(['ollama', 'searchapi_key'], 'ru')
    assert text.startswith('NEO проверил себя после запуска:\n• Ollama не отвечает') and 'Настройки → API-сервисы' in text
    assert kit.problems_text([], 'ru') == ''


def test_the_same_problem_is_said_once_a_week(tmp_path):
    config = _config(tmp_path)
    assert kit.not_told_recently(config, ['ollama'], now=1000.0) == ['ollama']
    assert kit.not_told_recently(config, ['ollama', 'ocr'], now=2000.0) == ['ocr']
    assert kit.not_told_recently(config, ['ollama'], now=1000.0 + 8 * 86400) == ['ollama']


@pytest.mark.parametrize('text', ['отчёт для разработчика', 'сделай отчёт для разработчика', 'developer report', 'make a developer report',
                                  'entwicklerbericht', 'rapport pour le développeur', 'informe para el desarrollador', 'rapporto per lo sviluppatore',
                                  'relatório para o programador'])
def test_report_phrases(text):
    from jarvis.runtime.everyday172 import _DEV_REPORT, _n
    assert _DEV_REPORT.match(_n(text))


def test_report_through_the_agent(tmp_path, monkeypatch):
    from jarvis.runtime import everyday172
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    monkeypatch.setattr(everyday172, '_show_in_explorer', lambda path: None)
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        try:
            session.reset_case('ru')
            turn = session.agent.handle('отчёт для разработчика')
            assert turn.tool_details == ['diagnostics.developer_report'] and turn.text.startswith('Отчёт для разработчика сохранён:')
            reports = list((Path(session.agent.config.paths.root) / 'reports').glob('neo_report_*.txt'))
            assert len(reports) == 1 and json.loads(reports[0].read_text(encoding='utf-8'))['setup']['settings_version'] >= 173
        finally:
            session.finish()


def test_checklist_guide_lists_every_feature_with_the_expected_result():
    # NEO 184: the checklist ships inside the program (docs/field_checklist_ru.md), so the test does not
    # depend on whether the user's archive also contains the Guides folder.
    internal = Path(__file__).resolve().parents[1]
    root = internal.parent
    candidates = [internal / 'docs' / 'field_checklist_ru.md',
                  *sorted((root / 'Guides').glob('NEO_173_Проверка*.md')), *sorted((root / 'Guide').glob('NEO_173_Проверка*.md'))]
    guide = next((path for path in candidates if path.is_file()), None)
    assert guide is not None
    text = guide.read_text(encoding='utf-8')
    rows = [line for line in text.splitlines() if line.startswith('| ') and '|' in line[2:] and not line.startswith('| №') and not line.startswith('|---')]
    assert len(rows) >= 40
    for word in ('отчёт для разработчика', 'что за ошибка', 'разбери загрузки', 'открой настройки звука', 'перемести это туда', 'скачай'):
        assert word in text.casefold(), word
