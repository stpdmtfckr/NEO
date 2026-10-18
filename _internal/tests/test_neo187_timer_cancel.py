"""NEO 187: TZ-TIMER-CANCEL-MULTILINGUAL-01 and REC-CANCEL-SCOPE-01 on the real Agent.

Timers are real TimerManager rows in a disposable profile; only the exact
permitted timer tool call may execute. No OS shutdown or app launch happens.
"""
import pytest

from jarvis.runtime import timer_cancel187
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.runtime.timers import timer_manager
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture
def world(tmp_path):
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        manager = timer_manager(session.config.paths.root, 'ru')
        try:
            yield session, manager
        finally:
            for item in manager.list():
                manager.cancel(item.id)
            session.finish()


def _say(session, manager, code, phrase, permit=None):
    session.reset_case(code)
    manager._lang = code
    if permit is not None:
        session._permitted = [permit]
    return session.agent.handle(phrase)


def _labels(manager):
    return sorted(item.label for item in manager.list())


def test_colon_negation_covers_the_whole_list(world):                          # A01 / A02
    session, manager = world
    manager.create(900, 'чай'); manager.create(600, 'суп')
    for phrase in ('Не запускай следующую команду: открой приложение калькулятор, удали таймер чай',
                   'Не запускай следующую команду: открой приложение калькулятор; отмени таймер чай'):
        turn = _say(session, manager, 'ru', phrase)
        assert _labels(manager) == ['суп', 'чай'] and not session.executions, turn.text


def test_two_real_intents_run_the_allowed_second_one(world):                   # A04
    session, manager = world
    tea = manager.create(900, 'чай'); manager.create(600, 'суп')
    turn = _say(session, manager, 'ru', 'Не открывай Steam, отмени таймер чай',
                ('timer', {'action': 'cancel', 'id': tea.id}))
    assert turn.success and _labels(manager) == ['суп']
    assert any(row.get('content') == 'Не открывай Steam, отмени таймер чай'
               for row in session.agent.history if row.get('role') == 'user')      # A08: original phrase


@pytest.mark.parametrize('phrase', ['Не отменяй таймер', 'не отменяй таймер чай', 'Запиши фразу «отмени таймер»',
                                    'Что будет, если я скажу «отмени таймер»?', 'Не выполняй «открой калькулятор, удали таймер чай»'])
def test_words_about_cancel_never_cancel(world, phrase):                       # A03 / L-ru-10..12
    session, manager = world
    manager.create(900, 'чай')
    _say(session, manager, 'ru', phrase)
    assert _labels(manager) == ['чай'] and not session.executions


CANCEL_ONE = {'ru': ['Отмени таймер', 'Пожалуйста, отмени таймер', 'Таймер, пожалуйста, отмени', 'Отмени таймре',
                     'ОТМЕНИ ТАЙМЕР', 'Отмени таймер!'],
              'en': ['Cancel the timer', 'CANCEL THE TIMER', 'please cancel the timer'],
              'de': ['Brich den Timer ab', 'Lösche den Timer'], 'fr': ['Annule le minuteur'],
              'es': ['Cancela el temporizador'], 'it': ['Annulla il timer'], 'pt': ['Cancela o temporizador']}


@pytest.mark.parametrize('code,phrase', [(c, p) for c, rows in CANCEL_ONE.items() for p in rows])
def test_single_timer_is_cancelled_and_reminder_kept(world, code, phrase):     # L-xx-01
    session, manager = world
    timer = manager.create(900, 'чай')
    manager.create(1800, 'позвонить', kind='reminder')
    turn = _say(session, manager, code, phrase, ('timer', {'action': 'cancel', 'id': timer.id}))
    assert turn.success, turn.text
    assert [item.label for item in manager.list()] == ['позвонить']


def test_several_timers_ask_which(world):
    session, manager = world
    manager.create(900, 'чай'); manager.create(600, 'суп')
    turn = _say(session, manager, 'ru', 'Отмени таймер')
    assert turn.outcome == 'clarification' and '«чай»' in turn.text and '«суп»' in turn.text
    assert len(manager.list()) == 2


@pytest.mark.parametrize('code,phrase', [('ru', 'Отмени все мои таймеры'), ('en', 'Cancel all my timers'),
                                         ('de', 'Brich alle meine Timer ab'), ('fr', 'Annule tous mes minuteurs'),
                                         ('es', 'Cancela todos mis temporizadores'), ('it', 'Annulla tutti i miei timer'),
                                         ('pt', 'Cancela todos os meus temporizadores')])
def test_cancel_all_only_timers(world, code, phrase):                          # L-xx-09
    session, manager = world
    manager.create(900, 'чай'); manager.create(600, 'суп')
    manager.create(1800, 'позвонить', kind='reminder')
    turn = _say(session, manager, code, phrase, ('timer', {'action': 'cancel_all'}))
    assert turn.success, turn.text
    assert [item.label for item in manager.list()] == ['позвонить']


def _shutdown_spec(code):
    return {'action': 'shutdown', 'target': {}, 'language': code, 'risk': 3, 'confirmed': True}


NEGATED_SHUTDOWN = {'ru': 'Не выключай компьютер', 'en': 'Don’t shut down the computer',
                    'de': 'Fahre den Computer nicht herunter', 'fr': 'N’éteins pas l’ordinateur',
                    'es': 'No apagues el ordenador', 'it': 'Non spegnere il computer', 'pt': 'Não desligues o computador'}


@pytest.mark.parametrize('code', LANGUAGES)
def test_negated_imperative_cancels_only_the_pending_matching_action(world, code):   # L-xx-02
    session, manager = world
    action = manager.create_action(600, 'Shutdown', _shutdown_spec(code))
    manager.create(900, 'чай')
    turn = _say(session, manager, code, NEGATED_SHUTDOWN[code])
    assert turn.success and action.id not in {item.id for item in manager.list()}
    assert _labels(manager) == ['чай'] and not session.executions


@pytest.mark.parametrize('code', LANGUAGES)
def test_negated_imperative_without_pending_action_does_nothing(world, code):
    session, manager = world
    manager.create(900, 'чай')
    turn = _say(session, manager, code, NEGATED_SHUTDOWN[code])
    assert not session.executions and _labels(manager) == ['чай']
    assert turn.outcome != 'clarification'


@pytest.mark.parametrize('code,phrase', [
    ('en', 'Don’t cancel the timer'), ('de', 'Brich den Timer nicht ab'), ('fr', 'N’annule pas le minuteur'),
    ('es', 'No canceles el temporizador'), ('it', 'Non annullare il timer'), ('pt', 'Não canceles o temporizador'),
    ('en', 'What happens if I say “cancel the timer”?'), ('de', 'Was passiert, wenn ich „Brich den Timer ab“ sage?'),
    ('fr', 'Que se passe-t-il si je dis « annule le minuteur » ?'), ('es', '¿Qué pasa si digo «cancela el temporizador»?'),
    ('it', 'Che cosa succede se dico «annulla il timer»?'), ('pt', 'O que acontece se eu disser «cancela o temporizador»?'),
    ('en', 'Write down the phrase “cancel the timer”'), ('it', 'Scrivi la frase «annulla il timer»'),
])
def test_keep_and_quoted_forms_in_all_languages(world, code, phrase):
    session, manager = world
    manager.create(900, 'чай')
    _say(session, manager, code, phrase)
    assert _labels(manager) == ['чай']
    assert not any(row['tool'] == 'timer' and row['arguments'].get('action', '').startswith('cancel') for row in session.calls)


def test_parser_matrix_is_finite_and_exact():
    assert timer_cancel187.parse('отмени таймер чай', 'ru').name == 'чай'
    assert timer_cancel187.parse('cancel the tea timer', 'en').name == 'tea'
    assert timer_cancel187.parse('отмени напоминание 2', 'ru') is None       # reminders keep their own command
    assert timer_cancel187.parse('открой таймер', 'ru') is None
    assert timer_cancel187.parse('не выключай свет', 'ru') is None


def test_default_cancel_follows_monotonic_remaining_not_stale_due_at(tmp_path, monkeypatch):   # TCO-R01/R02
    from jarvis.runtime.timers import TimerManager
    manager = TimerManager(); monkeypatch.setattr(manager, '_ensure_thread_locked', lambda: None)
    manager.configure(tmp_path, 'ru')
    a = manager.create(500, 'A'); b = manager.create(60, 'B')
    # The wall clock was corrected: B's stored due_at now looks later than A's,
    # but B still expires first by its monotonic deadline.
    with manager._lock:
        manager._timers[b.id].due_at = manager._timers[a.id].due_at + 1000
    assert manager.cancel() == 1
    assert [item.label for item in manager.list()] == ['A']
    assert manager.cancel(a.id) == 1 and manager.list() == []          # explicit ID unchanged
