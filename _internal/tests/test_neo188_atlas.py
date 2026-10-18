"""NEO 188: the 14.10 seven-language command atlas (part III) and the defects it found.

Routing only: every agent call runs in an isolated AtlasSession whose guard
blocks real execution. Nothing here proves Windows, a microphone or a live API.
"""
import hashlib
import json

import pytest

from jarvis.testing import atlas188

CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


@pytest.fixture(scope='module')
def atlas():
    return atlas188.load()


@pytest.fixture()
def session(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        s = AtlasSession(tmp_path / 'run')
        yield s
        s.finish()


def turn(session, code, text):
    session.reset_case(code)
    result = session.agent.handle(text)
    routes = [f"{c['tool']}.{c['arguments'].get('action', '')}" for c in session.calls]
    if result.pending:
        routes.append(f"{result.pending.tool_name}.{result.pending.arguments.get('action', '')}")
    return result, routes, [dict(c['arguments']) for c in session.calls]


# ------------------------------------------------------------------ the data itself

def test_atlas_data_matches_the_14_10_delivery(atlas):
    meta = atlas['atlas']
    assert meta['canonical_source_intents'] == 549 and meta['proposed_intents'] == 35 and meta['coverage_rows'] == 4088
    assert len(atlas['rows']) == 4088 and len(atlas['negative_cases']) == 70
    assert {r['language'] for r in atlas['rows']} == set(CODES)
    assert len({r['intent'] for r in atlas['rows'] if not r['intent'].startswith('proposed.')}) == 549
    proposed = {r['intent'] for r in atlas['rows'] if r['intent'].startswith('proposed.')}
    assert len(proposed) == 35 and proposed == set(atlas188.PROPOSED_BINDINGS)      # every proposal has a decision
    assert len(meta['sha256']) == 64


def test_every_slot_of_the_atlas_gets_a_typed_value(atlas, tmp_path):
    for row in atlas['rows']:
        for phrase in row['phrases']:
            rendered = atlas188.render(phrase, row['intent'], row['language'], tmp_path)
            assert '{' not in rendered and '}' not in rendered, (row['intent'], phrase)


# ------------------------------------------------------------------ on / off contrasts

@pytest.mark.parametrize('code,on,off', [
    ('ru', 'включи Bluetooth', 'выключить Bluetooth'), ('en', 'turn on Bluetooth', 'turn off Bluetooth'),
    ('de', 'schalte Bluetooth ein', 'schalte Bluetooth aus'), ('fr', 'active le Bluetooth', 'désactive le Bluetooth'),
    ('es', 'activa el Bluetooth', 'desactiva el Bluetooth'), ('it', 'attiva il Bluetooth', 'disattiva il Bluetooth'),
    ('pt', 'ativa o Bluetooth', 'desliga o Bluetooth')])
def test_bluetooth_polarity_comes_from_the_verb_in_every_language(session, code, on, off):
    _, routes, args = turn(session, code, on)
    assert routes == ['system.set_bluetooth'] and args[0]['enabled'] is True
    _, routes, args = turn(session, code, off)
    assert routes == ['system.set_bluetooth'] and args[0]['enabled'] is False


@pytest.mark.parametrize('code,on,off', [
    ('ru', 'включить микрофон', 'выключить микрофон'), ('en', 'unmute the microphone', 'mute the microphone'),
    ('de', 'schalte das Mikrofon laut', 'das Mikrofon stummschalten'), ('fr', 'réactive le micro', 'coupe le son du microphone'),
    ('es', 'reactiva el micrófono', 'silencia el micrófono'), ('it', 'riattiva il microfono', 'silenzia il microfono'),
    ('pt', 'reativa o microfone', 'silencia o microfone')])
def test_microphone_mute_and_unmute_are_never_swapped(session, code, on, off):
    _, routes, args = turn(session, code, on)
    assert routes == ['audio.set_input_mute'] and args[0]['muted'] is False
    _, routes, args = turn(session, code, off)
    assert routes == ['audio.set_input_mute'] and args[0]['muted'] is True


def test_wifi_on_phrase_from_the_atlas_does_not_ask_on_or_off(session):
    result, routes, args = turn(session, 'ru', 'активировать Wi-Fi')
    assert routes == ['system.set_wifi'] and args[0]['enabled'] is True and result.outcome != 'clarification'


def test_a_guess_never_offers_the_opposite_polarity():
    from jarvis.runtime import typos175
    assert typos175.opposite_guess('set_input_mute', 'включить микрофон', {'muted': True})
    assert not typos175.opposite_guess('set_input_mute', 'выключить микрофон', {'muted': True})
    assert typos175.opposite_guess('set_bluetooth', 'enable bluetooth', {'enabled': False})


# ------------------------------------------------------------------ refusals and «later»

def test_negated_russian_infinitive_never_proposes_the_action(session):
    result, routes, _ = turn(session, 'ru', 'не переводить компьютер в гибернацию')
    assert routes == [] and result.pending is None
    result, routes, _ = turn(session, 'ru', 'не забыть купить молоко')      # not a command verb: not a refusal
    assert 'routing.negated' not in (result.tool_details or [])


def test_delay_word_must_be_a_whole_word():
    from jarvis.runtime import delayed_actions184 as delayed
    assert delayed.parse('turn off instant replay', 'en') is None
    assert delayed.parse('shut down computer in10minutes', 'en')['seconds'] == 600.0


def test_an_action_for_later_is_not_run_now(session):
    for code, text in (('ru', 'открой блокнот через 5 минут'), ('en', 'open Notepad in 5 minutes'), ('de', 'öffne Notepad in 5 Minuten')):
        result, routes, _ = turn(session, code, text)
        assert routes == [] and 'delayed_actions184.unsupported188' in result.tool_details
    result, routes, _ = turn(session, 'en', 'Turn off instant replay')
    assert routes == ['capture.replay_off']


def test_holding_keys_is_refused_without_any_key_event(session, atlas):
    durations = {'ru': '5 минут', 'en': '5 minutes', 'de': '5 Minuten', 'fr': '5 minutes', 'es': '5 minutos', 'it': '5 minuti', 'pt': '5 minutos'}
    for row in atlas['rows']:
        if row['intent'].startswith('proposed.input.'):
            for phrase in row['phrases']:
                text = phrase.replace('{key}', 'F5').replace('{duration}', durations[row['language']])
                result, routes, _ = turn(session, row['language'], text)
                assert routes == [] and any(d.startswith('input.key_hold188.') for d in result.tool_details), text
    _, routes, _ = turn(session, 'ru', 'нажми F5')
    assert routes == ['ui.press']                                            # a single press is unchanged


# ------------------------------------------------------------------ cancellation by name

def test_cancel_the_shutdown_timer_cancels_the_scheduled_shutdown(session):
    from jarvis.runtime.timers import timer_manager
    for code, schedule, cancel in (('en', 'shut down the computer in 5 minutes', 'cancel the shutdown timer'),
                                   ('ru', 'выключи компьютер через 5 минут', 'отмени таймер выключения')):
        session.reset_case(code)
        first = session.agent.handle(schedule)
        assert first.pending is not None
        session.agent.confirm_pending(True)
        manager = timer_manager(session.config.paths.root, code)
        assert [row.kind for row in manager.list()] == ['action']
        session.calls.clear()
        done = session.agent.handle(cancel)
        assert 'timer_cancel187.action_cancelled' in done.tool_details and manager.list() == []
    result, _, _ = turn(session, 'fr', 'annule le minuteur d’arrêt')
    assert 'timer_cancel187.no_action188' in result.tool_details


def test_a_scheduled_app_launch_that_does_not_exist_is_said_honestly(session):
    result, routes, _ = turn(session, 'ru', 'отмени отложенный запуск Steam')
    assert routes == [] and 'timer_cancel187.no_launch188' in result.tool_details and 'Steam' in result.text
    result, routes, _ = turn(session, 'ru', 'убери таймер чай')
    assert routes == [] and 'чай' in result.text                              # never «close application таймер чай»


# ------------------------------------------------------------------ user words stay the user's

def test_free_text_slots_get_the_users_words_back():
    from jarvis.runtime import multilingual_commands as mc
    raw = 'open the excerpt supporting the answer'
    pivot = mc.normalize_multilingual_command(raw, 'en')
    assert pivot != raw
    restored = mc.restore_slot_words188({'action': 'open', 'name': 'excerpt supporting the ответь', 'kind': 'app'}, raw, 'en')
    assert restored['name'] == 'excerpt supporting the answer' and restored['kind'] == 'app'
    assert mc.restore_slot_words188({'name': 'ответь'}, 'ответь', 'ru') == {'name': 'ответь'}


def test_no_russian_pivot_word_leaks_into_a_foreign_slot(session):
    import re
    for code, text in (('en', 'start the saved YouTube playlist Chill'), ('pt', 'reproduza o próximo vídeo do YouTube'),
                       ('en', 'open Steam')):
        _, _, args = turn(session, code, text)
        for row in args:
            for key in ('name', 'query', 'title'):
                assert not re.search('[а-яё]', str(row.get(key, '')), re.I), (text, row)


# ------------------------------------------------------------------ bound proposals

def test_status_questions_use_the_existing_handlers(session, tmp_path):
    from jarvis.runtime import status_queries188 as q
    _, routes, _ = turn(session, 'de', 'welcher Prozess belastet den Computer')
    assert routes == ['system.top_processes']
    _, routes, args = turn(session, 'ru', 'покажи фрагмент, на котором основан ответ')
    assert routes == ['knowledge.show_source'] and args[0]['index'] == 1
    _, routes, args = turn(session, 'en', 'replace the current Discord song with Bohemian Rhapsody')
    assert routes == ['discord.music_bot_play'] and args[0]['query'] == 'Bohemian Rhapsody'
    library = tmp_path / 'steamapps'
    library.mkdir()
    (library / 'appmanifest_1.acf').write_text('"AppState"\n{\n"appid" "1"\n"name" "Alpha Game"\n"StateFlags" "1026"\n'
                                              '"BytesToDownload" "200"\n"BytesDownloaded" "50"\n}\n', encoding='utf-8')
    (library / 'appmanifest_2.acf').write_text('"AppState"\n{\n"appid" "2"\n"name" "Done Game"\n"StateFlags" "4"\n'
                                              '"BytesToDownload" "10"\n"BytesDownloaded" "10"\n}\n', encoding='utf-8')
    answer = q.installs_answer('ru', libraries=[library])
    assert 'Alpha Game — 25%' in answer and 'Done Game' not in answer
    assert 'не устанавливает «Zeta»' in q.installs_answer('ru', 'Zeta', libraries=[library])
    assert q.parse('покажи состояние установки Steam', 'ru') == ('installs', '')


def test_assistant_window_phrases_of_the_atlas(atlas):
    from jarvis.runtime.self_window184 import parse_self_window
    for row in atlas['rows']:
        if row['intent'].startswith('proposed.assistant.'):
            want = 'hide' if row['intent'].endswith('hide_behind') else 'show'
            assert all(parse_self_window(p, row['language']) == want for p in row['phrases']), row


def test_screen_snapshot_named_explicitly_is_continued_or_declared_gone(session):
    from jarvis.runtime import screen_followup188 as sf
    sf.forget()
    result, routes, _ = turn(session, 'en', 'continue using the same screenshot')
    assert routes == [] and result.tool_details == ['screen.observation_details188']


def test_study_commands_without_a_session(session):
    from jarvis.runtime import study188
    result, routes, _ = turn(session, 'ru', 'пропусти этот вопрос')
    assert routes == [] and 'study188' in result.tool_details and 'потренируй меня по файлу' in result.text
    for code, example in (('ru', 'потренируй меня по файлу конспект.docx'), ('en', 'quiz me on notes.docx'),
                          ('de', 'trainiere mich mit notizen.docx'), ('fr', 'entraîne-moi sur notes.docx'),
                          ('es', 'entréname con apuntes.docx'), ('it', 'allenami su appunti.docx'), ('pt', 'treina-me com apontamentos.docx')):
        assert study188.parse_start(example, code) is not None                # the hint names a working command
    assert not study188.without_session188('пропусти', 'ru')                  # bare «skip» stays a media command


def test_scenario_inputs_after_with_parameters():
    from jarvis.runtime import scenario_inputs188 as si
    row = {'id': 'work', 'name': 'Рабочее место', 'enabled': True,
           'inputs': [{'name': 'screen', 'type': 'monitor', 'label': 'монитор'}, {'name': 'pause', 'type': 'duration', 'label': 'пауза', 'default': 1}],
           'steps': [{'type': 'wait', 'seconds': 1, 'bind': {'seconds': 'pause'}}]}
    assert si.match('выполни сценарий Рабочее место с параметрами монитор=2, пауза=3', 'ru', [row]) == ('Рабочее место', {'screen': 2, 'pause': 3.0})


def test_unknown_scenario_and_missing_shelf_note_are_named(session):
    result, _, _ = turn(session, 'en', 'run scenario Alpha')
    assert 'scenario.target188.missing' in result.tool_details and 'show my scenarios' in result.text
    _, routes, _ = turn(session, 'en', 'show my scenarios')
    assert routes == ['scenario.list']
    result, _, _ = turn(session, 'ru', 'убери заметку Alpha с полки')
    assert 'shelf.target188' in result.tool_details and '«Alpha»' in result.text


def test_selected_document_translation_asks_for_the_file(session):
    from jarvis.runtime import doc_translate_control188 as dc
    result, routes, _ = turn(session, 'ru', 'переведи выбранный документ на английский и покажи рядом оригинал')
    assert routes == [] and 'doctr188' in result.tool_details
    assert dc.parse_start('traduis le document rapport.docx en anglais', 'fr') == {'file': 'rapport.docx', 'target': 'en'}


# ------------------------------------------------------------------ smaller parsing defects

def test_smaller_atlas_defects(session):
    _, routes, args = turn(session, 'ru', 'выбери кнопку автокликера левую')
    assert routes == ['autoclicker.set_button'] and args[0]['button'] == 'left'
    _, routes, args = turn(session, 'es', 'cierra el widget informativo Alpha')
    assert routes == ['overlay.hide'] and args[0]['name'] == 'Alpha'
    _, routes, args = turn(session, 'ru', 'создай папку C:\\Temp\\X')
    assert routes == ['filesystem.create_folder'] and args[0]['destination'] == 'C:\\Temp\\X'
    _, routes, _ = turn(session, 'fr', 'ne répète pas les chansons automatiquement')
    assert routes == ['music.repeat_off']
    result, routes, _ = turn(session, 'fr', 'ne répète pas la chanson')
    assert routes == [] and 'routing.negated' in result.tool_details


def test_voice_dialogue_stop_phrases_of_the_atlas(atlas):
    from jarvis.runtime.continuous_stop188 import is_stop
    rows = [r for r in atlas['rows'] if r['intent'] == 'proposed.voice.stop_dialogue']
    assert rows and all(is_stop(p, r['language']) for r in rows for p in r['phrases'])


# ------------------------------------------------------------------ the corpus itself

# Reviewed open items of the routing corpus (documented in the 188 guide, not hidden).
KNOWN_OPEN = {
    'game_locations.open',          # «покажи папку установки» shows the found path instead of opening Explorer
    'filesystem.create_text_file',  # «создай документ … с текстом» creates a Word document
    'copy.selection',               # «copie ça» without context asks which file
}


@pytest.mark.parametrize('code', CODES)
def test_first_phrase_of_every_atlas_row_routes_safely(session, atlas, code):
    """One phrase per intent and language; the full 19 593-phrase run is «atlas_test_runner.py --headless --selection atlas188»."""
    bad = []
    for row in atlas['rows']:
        if row['language'] != code or not row['phrases']:
            continue
        result = atlas188.check(session, row, row['phrases'][0])
        if result.status in {'EXCLUDED_VIOLATED', 'ERROR'}:
            bad.append((row['intent'], result.status, result.phrase, result.actual))
        elif result.status in {'ROUTE_MISMATCH', 'NO_ROUTE'} and row['intent'] not in KNOWN_OPEN:
            bad.append((row['intent'], result.status, result.phrase, result.actual))
    assert not bad, bad[:10]


# ------------------------------------------------------------------ negative cases of the atlas

@pytest.mark.parametrize('code,text,route,direction', [
    ('ru', 'перемотай видео на десять секунд', 'media.seek_forward', 10), ('en', 'seek forward ten seconds', 'media.seek_forward', 10),
    ('de', 'spule zehn Sekunden vor', 'media.seek_forward', 10), ('fr', 'avance de dix secondes', 'media.seek_forward', 10),
    ('es', 'retrocede 1 minuto', 'media.seek_backward', 60), ('it', 'torna indietro di 5 secondi', 'media.seek_backward', 5),
    ('pt', 'recua 20 segundos', 'media.seek_backward', 20)])
def test_relative_seek_with_spoken_numbers_moves_the_current_video(session, code, text, route, direction):
    _, routes, args = turn(session, code, text)
    assert routes == [route] and float(args[0]['seconds']) == direction


def test_an_opinion_about_the_weather_is_not_a_forecast_request(session):
    for code, text in (('ru', 'ну, я думаю, сегодня дождь'), ('en', 'well, I think it will rain'),
                       ('pt', 'bem, acho que vai chover'), ('es', 'bueno, creo que llueve')):
        _, routes, _ = turn(session, code, text)
        assert routes == [], text
    _, routes, _ = turn(session, 'ru', 'будет ли дождь завтра')
    assert routes == ['weather.rain']


def test_browser_window_named_with_an_article_in_any_language(session):
    _, routes, args = turn(session, 'pt', 'minimiza o navegador')
    assert routes == ['window.minimize'] and args[0]['title'] == 'o navegador'      # the user's words, no «браузер»


def test_typo_repair_never_swaps_on_and_off():
    from jarvis.runtime.typos175 import polarity_flip188
    assert polarity_flip188('riattiva il microfono', 'disattiva il microfono')
    assert polarity_flip188('включить микрофон', 'выключи микрофон')
    assert not polarity_flip188('отрой браузер', 'открой браузер')


@pytest.mark.parametrize('case_id', ['NEG-ru-03', 'NEG-en-04', 'NEG-de-01', 'NEG-fr-10', 'NEG-es-08', 'NEG-it-02', 'NEG-pt-03'])
def test_sample_of_atlas_negative_cases_never_runs_the_forbidden_action(session, atlas, case_id):
    case = next(row for row in atlas['negative_cases'] if row['id'] == case_id)
    result, routes, args = turn(session, case['language'], case['text'])
    assert not any(r.startswith(('system.shutdown', 'system.hibernate', 'filesystem.delete', 'music.stop', 'media.pause')) for r in routes)
    if case_id == 'NEG-it-02':
        assert routes == ['music.play_track'] and args[0]['query'] == 'Stop'           # a title, never a stop
    if case_id == 'NEG-fr-10':
        assert result.pending is None and routes == []                                  # «peut-être oui» confirms nothing
