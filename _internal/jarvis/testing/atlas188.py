"""NEO 188: the 14.10 seven-language phrase atlas as a routing corpus.

The atlas phrases are source declarations and design proposals, not phrases
checked by native speakers or a real ASR. Running them proves only routing and
argument validation in an isolated profile; execution is always blocked by the
AtlasSession guard. Proposed IDs are compared against the runtime handler that
build 187/188 bound them to, or reported as policy exclusions.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import gzip
import json
from pathlib import Path
import re
from typing import Iterable, Iterator

DATA = Path(__file__).resolve().parent / 'data' / 'atlas188.json.gz'
CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_SLOT = re.compile(r'\{([A-Za-z_][A-Za-z_0-9]*\??)\}')


def load() -> dict:
    with gzip.open(DATA, 'rt', encoding='utf-8') as handle:
        return json.load(handle)


def _per_locale(*values: str) -> dict[str, str]:
    return dict(zip(CODES, values))


# One typed, plausible value per slot. Literal values (titles, names) are the
# same in every locale; words the grammar has to agree with are localized.
_LOCALIZED = {
    'city': _per_locale('Москве', 'London', 'Berlin', 'Paris', 'Madrid', 'Roma', 'Lisboa'),
    'text': _per_locale('привет мир', 'hello world', 'hallo Welt', 'bonjour le monde', 'hola mundo', 'ciao mondo', 'olá mundo'),
    'body': _per_locale('встречаемся в пять', 'see you at five', 'bis um fünf', 'à cinq heures', 'nos vemos a las cinco', 'ci vediamo alle cinque', 'até às cinco'),
    'target_language': _per_locale('английский', 'German', 'Englisch', 'anglais', 'inglés', 'inglese', 'inglês'),
    'date': _per_locale('завтра', 'tomorrow', 'morgen', 'demain', 'mañana', 'domani', 'amanhã'),
    'duration': _per_locale('5 минут', '5 minutes', '5 Minuten', '5 minutes', '5 minutos', '5 minuti', '5 minutos'),
    'label': _per_locale('позвонить маме', 'call mom', 'Mama anrufen', 'appeler maman', 'llamar a mamá', 'chiamare la mamma', 'ligar para a mãe'),
    'action': _per_locale('открой блокнот', 'open Notepad', 'öffne Notepad', 'ouvre Notepad', 'abre Notepad', 'apri Notepad', 'abre o Notepad'),
    'button': _per_locale('левую', 'left', 'links', 'gauche', 'izquierdo', 'sinistro', 'esquerdo'),
    'setting': _per_locale('тему', 'theme', 'Design', 'thème', 'tema', 'tema', 'tema'),
    'question': _per_locale('кто автор', 'who is the author', 'wer ist der Autor', "qui est l'auteur", 'quién es el autor', "chi è l'autore", 'quem é o autor'),
    'chart_type': _per_locale('столбчатую', 'bar', 'Balken', 'en barres', 'de barras', 'a barre', 'de barras'),
    'timer': _per_locale('чай', 'tea', 'Tee', 'thé', 'té', 'tè', 'chá'),
    'instruction': _per_locale('кнопка поиска', 'the search button', 'die Suchtaste', 'le bouton de recherche', 'el botón de búsqueda', 'il pulsante di ricerca', 'o botão de pesquisa'),
    'description': _per_locale('калькулятор', 'a calculator', 'ein Taschenrechner', 'une calculatrice', 'una calculadora', 'una calcolatrice', 'uma calculadora'),
}
_LITERAL = {
    'name': 'Alpha', 'query': 'Alpha Centauri', 'title': 'Alpha', 'content': 'Alpha', 'index': '2', 'value': '30',
    'channel': 'general', 'playlist': 'Chill', 'seconds': '30', 'key': 'F5', 'target': 'Chrome', 'volume': '40',
    'input': 'Mic', 'scene': 'Game', 'number': '3', 'application': 'Chrome', 'device_name': 'Speakers',
    'url': 'https://example.org/', 'topic': 'Python', 'subject': 'Alpha', 'to': 'qa@example.invalid',
    'new_name': 'renamed.txt', 'event_id': 'evt42', 'monitor': '2', 'server': 'Alpha', 'new_text': 'Beta',
    'old_text': 'Alpha', 'time': '15:00', 'x': '100', 'y': '200', 'keys': 'ctrl+c', 'position_seconds': '90',
    'zoom': '120', 'host': 'example.org', 'font_size': '14', 'sender': 'Alex', 'reply_to_message_id': '42',
    'message_id': '42', 'id': '1', 'height': '600', 'width': '800', 'app': 'Steam', 'app?': 'Steam',
    'track': 'Bohemian Rhapsody', 'document_a': 'a.docx', 'document_b': 'b.docx', 'source_cell': 'A1',
    'target_range': 'B1:B10', 'scenario': 'Morning', 'parameters': 'pause=3', 'field': 'Alpha',
    'parameter': 'duration',
}
# The same slot means something else in a few domains.
_BY_DOMAIN = {
    ('application', 'name'): 'Notepad', ('process', 'name'): 'Notepad', ('window', 'name'): 'Notepad',
    ('window', 'title'): 'Notepad', ('window_advanced', 'name'): 'Notepad', ('browser', 'name'): 'Chrome',
    ('music', 'query'): 'Bohemian Rhapsody', ('spotify', 'query'): 'Bohemian Rhapsody',
    ('yandex_music', 'query'): 'Bohemian Rhapsody', ('discord', 'query'): 'Bohemian Rhapsody',
    ('music', 'title'): 'Bohemian Rhapsody', ('spotify', 'title'): 'Bohemian Rhapsody',
    ('yandex_music', 'title'): 'Bohemian Rhapsody', ('memory', 'key'): 'code', ('memory', 'value'): '42',
    ('browser_nav', 'text'): 'example.org', ('telegram_messages', 'target'): 'Alex',
    ('overlay', 'new_name'): 'Beta', ('timer', 'id'): '1',
}


def slot_value(intent: str, slot: str, code: str, workspace: Path) -> str:
    domain = intent.split('.', 1)[0]
    if slot in {'source', 'path'}:
        name = 'source.py' if intent == 'developer.run_python_file' else 'source.txt'
        return str(workspace / name)
    if slot in {'destination', 'folder'}:
        return str(workspace / 'destination')
    if (domain, slot) in _BY_DOMAIN:
        return _BY_DOMAIN[(domain, slot)]
    if slot in _LOCALIZED:
        return _LOCALIZED[slot][code]
    return _LITERAL.get(slot, 'Alpha')


def render(phrase: str, intent: str, code: str, workspace: Path) -> str:
    return _SLOT.sub(lambda match: slot_value(intent, match.group(1), code, workspace), phrase)


# Proposed design IDs → what builds 187/188 bound them to. Each value is
# (kind, accepted): 'route' accepts any of the tool.action routes or turn
# details listed; 'excluded' is a policy exclusion (holding keys) that must
# produce no input event; 'context' needs state the isolated profile does not
# have (a pending question, a selected file); 'voice' exists only in the voice
# channel; 'unbound' has no handler yet and is reported as such.
PROPOSED_BINDINGS: dict[str, tuple[str, tuple[str, ...]]] = {
    'proposed.input.hold': ('excluded', ('input.key_hold188.hold',)),
    'proposed.input.hold_for': ('excluded', ('input.key_hold188.hold',)),
    'proposed.input.release': ('excluded', ('input.key_hold188.release',)),
    'proposed.input.release_all': ('excluded', ('input.key_hold188.release',)),
    'proposed.assistant.hide_behind': ('route', ('assistant.window.hide',)),
    'proposed.assistant.show_front': ('route', ('assistant.window.show',)),
    'proposed.discord.play_now': ('route', ('discord.music_bot_play',)),
    'proposed.document.compare': ('route', ('knowledge.compare',)),
    'proposed.document.show_evidence': ('route', ('knowledge.show_source',)),
    'proposed.document.translate_aligned': ('route', ('doctr188',)),
    'proposed.installations.status': ('route', ('status188.installs',)),
    'proposed.monitor.spike_status': ('route', ('system.top_processes',)),
    'proposed.office.copy_formula': ('route', ('office.chart.preview',)),      # the shared preview of NEO-OFFICE-001/002
    'proposed.privacy.revoke_folder': ('route', ('knowledge.remove_folder',)),
    'proposed.scenario.run_typed': ('route', ('scenario.run',)),
    'proposed.screen.continue_observation': ('route', ('screen.observation_details188',)),
    'proposed.study.start': ('route', ('study188',)),
    'proposed.study.repeat': ('route', ('study188',)),
    'proposed.study.skip': ('route', ('study188',)),
    'proposed.study.help': ('route', ('study188',)),
    'proposed.timer.cancel_named': ('route', ('timer.cancel', 'timer_cancel187.not_found', 'timer_cancel187.none')),
    'proposed.timer.cancel_shutdown': ('route', ('timer.cancel', 'timer_cancel187.negated', 'timer_cancel187.no_action188')),
    'proposed.timer.cancel_sleep': ('route', ('timer.cancel', 'timer_cancel187.negated', 'timer_cancel187.no_action188')),
    'proposed.timer.cancel_hibernate': ('route', ('timer.cancel', 'timer_cancel187.negated', 'timer_cancel187.no_action188')),
    'proposed.timer.cancel_app_start': ('route', ('timer_cancel187.negated', 'timer_cancel187.no_launch188')),
    'proposed.timer.schedule_action': ('route', ('delayed_actions184.unsupported188',)),
    'proposed.video.next': ('route', ('media.next',)),
    'proposed.video.previous': ('route', ('media.previous',)),
    'proposed.youtube.saved_playlist': ('route', ('youtube.named_playlist', 'media.play_search')),
    'proposed.dialogue.correct_parameter': ('context', ()),
    'proposed.dialogue.explain_field': ('context', ()),
    'proposed.document.ask': ('context', ()),
    'proposed.office.chart_selected': ('context', ()),
    'proposed.voice.stop_dialogue': ('voice', ()),
    'proposed.settings.find': ('unbound', ()),
}
# Source IDs whose runtime owner is a different, equivalent handler (reviewed):
EQUIVALENT: dict[str, tuple[str, ...]] = {
    'browser.open': ('application.open',),               # a named browser is opened as that application
    'filesystem.find': ('search.files',),                # the dedicated file search tool owns file lookup
    'translate.translate': ('translate.',),              # the translate tool takes no action argument
    'overlay.close': ('overlay.hide',),                  # overlay_state treats hide and close alike
    'browser_advanced.restore_closed': ('apps.browser',),  # the browser skill sends Ctrl+Shift+T (reopen_tab)
    'screen.translate': ('explain.translate.',),         # the local explain/translate feature (no screen in QA)
    'ui.read': ('explain.read.',),
    'timer.cancel': ('timer_cancel187.',),               # the local multilingual cancel answers itself
}
# Read-only state probes the clarification code makes; never an effect.
_PROBES = {'discord.music_queue_status', 'music.background_status'}


@dataclass
class Result:
    intent: str
    locale: str
    phrase: str
    status: str
    expected: str = ''
    actual: list[str] = field(default_factory=list)
    response: str = ''


def expected_route(row: dict) -> str:
    return str(row.get('runtime') or '')


def cases(data: dict, locales: Iterable[str] = CODES, *, proposed: bool = True) -> Iterator[tuple[dict, str]]:
    wanted = set(locales)
    for row in data['rows']:
        if row['language'] not in wanted:
            continue
        if not proposed and row['intent'].startswith('proposed.'):
            continue
        for phrase in row['phrases']:
            yield row, phrase


def _routes(calls: list[dict]) -> list[str]:
    return [f"{call['tool']}.{call['arguments'].get('action', '')}" for call in calls]


def _seed_shelf(session, intent: str) -> None:
    """Exactly one owned shelf object «Alpha» exists, so the route binds a real target."""
    from jarvis.ui.shelf_store import ShelfStore
    store = ShelfStore(session.config.paths.root)
    for item in store.list():
        if str(item.get('title', '')) == 'Alpha':
            store.delete(item['id'])
    if intent == 'shelf.check':
        store.create('checklist', 'Alpha', items=[{'text': 'one', 'checked': False}, {'text': 'two', 'checked': False}])
    else:
        store.create('note', 'Alpha', 'Alpha text')


_SCENARIOS = [
    {'id': 'alpha', 'name': 'Alpha', 'enabled': True, 'steps': [{'type': 'wait', 'seconds': 1}]},
    {'id': 'morning', 'name': 'Morning', 'enabled': True,
     'inputs': [{'name': 'pause', 'type': 'duration', 'label': 'pause', 'default': 1}],
     'steps': [{'type': 'wait', 'seconds': 1, 'bind': {'seconds': 'pause'}}]},
]


def check(session, row: dict, phrase: str) -> Result:
    """Route one phrase with execution blocked; compare with the atlas row."""
    scenarios = session.config.data.setdefault('scenarios', {})
    seeded = row['intent'].startswith(('scenario.', 'proposed.scenario.'))
    previous = scenarios.get('items')
    if seeded:
        scenarios['items'] = [dict(item) for item in _SCENARIOS]
    try:
        return _check(session, row, phrase)
    finally:
        if seeded:
            scenarios['items'] = previous if previous is not None else []


def _check(session, row: dict, phrase: str) -> Result:
    code = row['language']
    session.reset_case(code)
    if row['intent'] in {'shelf.check', 'shelf.delete', 'shelf.update'}:
        _seed_shelf(session, row['intent'])
    text = render(phrase, row['intent'], code, session.workspace)
    turn = session.agent.handle(text)
    calls = list(session.calls)
    if turn.pending:
        calls.append({'tool': turn.pending.tool_name, 'arguments': turn.pending.arguments})
    expected = expected_route(row)
    actual = [route for route in _routes(calls) if route not in _PROBES or route == expected]
    details = [str(item) for item in (turn.tool_details or [])]
    binding = PROPOSED_BINDINGS.get(row['intent'])
    state = session.agent.conversations.get('desktop')
    asking = bool(state.pending_intent or state.pending_shell or session.agent._pending_target_choice)
    pending = state.pending_intent
    asked = f"{pending.tool}.{pending.arguments.get('action', '')}" if pending is not None else ''
    if binding and binding[0] == 'excluded':
        pressed = [route for route in actual if route.startswith(('ui.', 'input_control.'))]
        status = 'EXCLUDED_NO_ACTION' if not pressed and any(d in details for d in binding[1]) else 'EXCLUDED_VIOLATED'
        return Result(row['intent'], code, text, status, '|'.join(binding[1]), actual + details, turn.text)
    if binding and binding[0] in {'context', 'voice', 'unbound'}:
        status = {'context': 'NEEDS_CONTEXT', 'voice': 'VOICE_CHANNEL_ONLY', 'unbound': 'PROPOSED_UNBOUND'}[binding[0]]
        return Result(row['intent'], code, text, status, '', actual + details, turn.text)
    accepted = set(binding[1]) if binding else {expected} | set(EQUIVALENT.get(row['intent'], ()))
    def ok(item: str) -> bool:
        return item in accepted or any(a.endswith('.') and item.startswith(a) for a in accepted)
    if not accepted - {''}:
        status = 'PROPOSED_UNBOUND'
    elif actual and all(ok(route) for route in actual):
        status = 'ROUTE_PASS' if expected in actual or binding else 'ROUTE_EQUIVALENT'
    elif not actual and any(ok(item) for item in details):
        status = 'ROUTE_PASS' if binding or expected in details else 'ROUTE_EQUIVALENT'
    elif actual:
        status = 'ROUTE_MISMATCH'
    elif asked and (ok(asked) or (asked.endswith('.') and expected.startswith(asked))):
        status = 'CLARIFY_SAME_ROUTE'          # the right handler asks for a slot the phrase did not give
        actual = [asked]
    elif asking:
        status = 'NEEDS_INPUT'
        actual = [asked] if asked else actual
    else:
        status = 'NO_ROUTE'
    return Result(row['intent'], code, text, status, '|'.join(sorted(accepted)), actual + details, turn.text)


def summarize(results: Iterable[Result]) -> dict:
    by_locale: dict[str, Counter] = {}
    for result in results:
        by_locale.setdefault(result.locale, Counter())[result.status] += 1
    return {code: dict(counter) for code, counter in by_locale.items()}
