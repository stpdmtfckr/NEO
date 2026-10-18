"""NEO 168: spoken on-screen, browser and copy commands reach the right tool in every language.

Real parser and Agent in an isolated profile; every tool execution is blocked.
"""
from __future__ import annotations

import pytest

from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

CASES = [
    ('ru', 'покажи номера', 'ui', {'action': 'hints'}),
    ('ru', 'нажми номер 3', 'ui', {'action': 'click_number', 'number': '3'}),
    ('ru', 'открой первую ссылку', 'ui', {'action': 'click_link', 'index': 'первую'}),
    ('ru', 'напечатай привет, как дела', 'ui', {'action': 'type', 'text': 'привет, как дела'}),
    ('ru', 'нажми ещё раз', 'ui', {'action': 'again'}),
    ('ru', 'прокрути вниз', 'ui', {'action': 'scroll_down'}),
    ('ru', 'обнови страницу', 'browser_nav', {'action': 'reload'}),
    ('ru', 'скопируй свой ответ', 'copy', {'action': 'reply'}),
    ('en', 'click number 12', 'ui', {'action': 'click_number', 'number': '12'}),
    ('en', 'press ctrl s', 'ui', {'action': 'press', 'key': 'ctrl s'}),
    ('en', 'scroll to the bottom', 'ui', {'action': 'scroll_bottom'}),
    ('en', 'copy the page address', 'copy', {'action': 'url'}),
    ('de', 'zeige Nummern', 'ui', {'action': 'hints'}),
    ('de', 'klicke die zweite Schaltfläche', 'ui', {'action': 'click_button', 'index': 'zweite'}),
    ('fr', 'ouvre le premier lien', 'ui', {'action': 'click_link', 'index': 'premier'}),
    ('fr', 'défile vers le bas', 'ui', {'action': 'scroll_down'}),
    ('es', 'muestra los números', 'ui', {'action': 'hints'}),
    ('es', 'copia tu respuesta', 'copy', {'action': 'reply'}),
    ('it', 'mostra i numeri', 'ui', {'action': 'hints'}),
    ('it', 'ricarica la pagina', 'browser_nav', {'action': 'reload'}),
    ('pt', 'mostra os números', 'ui', {'action': 'hints'}),
    ('pt', 'desloca para baixo', 'ui', {'action': 'scroll_down'}),
]


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('neo168') / 'run')
        yield value
        value.finish()


@pytest.mark.parametrize('code,phrase,tool,arguments', CASES)
def test_command_routes_to_the_168_tool(session, code, phrase, tool, arguments):
    session.reset_case(code)
    session.agent.handle(phrase)
    assert session.calls, phrase
    call = session.calls[0]
    assert call['tool'] == tool
    for key, value in arguments.items():
        assert str(call['arguments'].get(key, '')).casefold() == value.casefold(), (key, call['arguments'])


@pytest.mark.parametrize('code,phrase', [('ru', 'вставь в файл заметки'), ('en', 'paste into the file notes')])
def test_appending_to_a_file_waits_for_confirmation(session, code, phrase):
    session.reset_case(code)
    turn = session.agent.handle(phrase)
    assert not session.calls
    assert turn.pending is not None and turn.pending.tool_name == 'copy'
    assert turn.pending.arguments['action'] == 'paste_file'
