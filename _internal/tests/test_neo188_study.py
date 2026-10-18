"""NEO 188 NEO-STUDY-001: a grounded practice session over one chosen allowed document."""
import json
from unittest.mock import Mock

import pytest

from jarvis.runtime import study188 as st
from jarvis.runtime import study_control188 as control
from jarvis.runtime.voice_followup156 import pending_slot
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
TEXT = ('Глава 1. Фотосинтез происходит в хлоропластах растительных клеток под действием света. '
        'Растения поглощают углекислый газ и выделяют кислород в атмосферу планеты. '
        'Первая фотография была сделана в 1826 году французским изобретателем Ньепсом. '
        'Глава 2. Митохондрии вырабатывают энергию для большинства процессов внутри клетки. '
        'Вода закипает при температуре 100 градусов на уровне моря обычно.')


@pytest.fixture
def session(tmp_path):
    with external_io_blocked():
        s = AtlasSession(tmp_path / 'run')
        s.reset_case('ru')
        docs = tmp_path / 'docs'
        docs.mkdir()
        (docs / 'конспект.txt').write_text(TEXT, encoding='utf-8')
        (docs / 'пусто.txt').write_text('коротко.', encoding='utf-8')
        from jarvis.runtime import knowledge169
        service = knowledge169.service(s.agent.config)
        service.add_folder(docs)
        service.index.update_folders([str(docs)], pause=0)
        s.docs = docs
        s.service = service
        yield s
        s.finish()


def say(s, text):
    return s.agent.handle(text)


def current(s):
    return control.active(s.agent)


def test_stu01_one_question_at_a_time_each_with_its_fragment(session):
    first = say(session, 'потренируй меня по конспекту: три вопроса')
    assert first.tool_details == ['study188'] and first.text.count('❓') == 1 and 'Вопрос 1 из 3' in first.text
    sess = current(session)
    assert sess.count == 3 and all(q.location and q.sentence for q in sess.questions)
    assert 'Вопрос 2' not in first.text
    nxt = say(session, sess.questions[0].answer)
    assert 'Вопрос 2 из 3' in nxt.text and nxt.text.count('❓') == 1 and '📄 Фрагмент: конспект.txt' in nxt.text


def test_stu02_feedback_is_distinct_and_grounded():
    q = st.Question(1, 'Фотосинтез происходит в хлоропластах растительных клеток под действием света.', 'хлоропластах', 'lines:1-1', 0)
    assert st.check(q, 'хлоропластах', 'ru') == 'matched'
    assert st.check(q, 'хлоропластох', 'ru') == 'close'            # one letter off (recognition/typo)
    assert st.check(q, 'митохондриях', 'ru') == 'wrong'
    assert st.check(q, 'не в хлоропластах', 'ru') == 'unclear'      # a negation is clarified, never marked wrong
    n = st.Question(2, 'Первая фотография была сделана в 1826 году.', '1826', '', 0)
    assert st.check(n, '1826', 'ru') == 'matched' and st.check(n, '1827', 'ru') == 'wrong'
    assert st.check(n, 'тысяча восемьсот двадцать шесть', 'ru') in {'matched', 'unclear'}


def test_stu03_hint_does_not_reveal_and_repeat_keeps_the_number(session):
    say(session, 'потренируй меня по конспекту: два вопроса')
    q = current(session).current
    hint = say(session, 'подсказка').text
    assert q.answer not in hint and q.answer[0] in hint
    hint2 = say(session, 'подсказка').text
    assert q.answer not in hint2
    again = say(session, 'повтори вопрос другими словами').text
    assert 'Вопрос 1 из 2' in again and current(session).current is q and q.attempts == 0


def test_stu04_skip_is_not_an_error_and_retry_only_on_request_once(session):
    say(session, 'потренируй меня по конспекту: два вопроса')
    skipped = say(session, 'пропусти').text
    assert 'не ошибка' in skipped and 'Вопрос 2 из 2' in skipped
    end = say(session, current(session).current.answer).text
    assert 'пропущено — 1' in end and 'не совпало — 0' in end and 'повторим пропущенные' in end
    again = say(session, 'повторим пропущенные').text
    assert 'Вопрос 1 из 2' in again
    say(session, 'пропусти')
    assert 'нет' in say(session, 'повторим пропущенные').text.casefold()    # no endless loop


def test_stu05_pause_stops_questions_and_listening_resume_keeps_position(session):
    say(session, 'потренируй меня по конспекту: три вопроса')
    say(session, current(session).current.answer)
    assert pending_slot(session.agent, 'ru') is not None                    # an asked question awaits a free answer
    paused = say(session, 'пауза')
    assert 'на паузе' in paused.text and pending_slot(session.agent, 'ru') is None
    other = say(session, 'хлоропластах')
    assert other.tool_details != ['study188']                              # a paused session takes no answers
    resumed = say(session, 'продолжи тренировку')
    assert 'Вопрос 2 из 3' in resumed.text


def test_stu06_cancel_never_resumes_and_commands_are_not_answers(session):
    say(session, 'потренируй меня по конспекту: два вопроса')
    paused = say(session, 'открой браузер')
    assert 'команда, а не ответ' in paused.text and current(session).state == 'paused'
    say(session, 'продолжи тренировку')
    quoted = say(session, '«открой терминал»')
    assert quoted.tool_details == ['study188'] and 'В выбранном тексте' in quoted.text   # a quoted command is just an answer
    cancelled = say(session, 'отмени тренировку')
    assert 'отменена' in cancelled.text and current(session) is None
    assert say(session, 'хлоропластах').tool_details != ['study188']


def test_stu07_saved_place_resumes_only_the_same_unchanged_source(session):
    say(session, 'потренируй меня по конспекту: три вопроса')
    say(session, current(session).current.answer)
    saved = say(session, 'сохрани место, продолжу завтра').text
    assert 'вопрос 2 из 3' in saved and 'без ваших ответов' in saved
    say(session, 'отмени тренировку')
    resumed = say(session, 'продолжи тренировку по конспект.txt')
    assert 'Вопрос 2 из 3' in resumed.text and current(session).questions[0].status == 'matched'
    say(session, 'отмени тренировку')
    (session.docs / 'конспект.txt').write_text(TEXT + ' Новое предложение для изменения версии документа здесь.', encoding='utf-8')
    session.service.index.update_folders([str(session.docs)], pause=0)
    changed = say(session, 'продолжи тренировку по конспект.txt').text
    assert 'изменился' in changed and current(session) is None


def test_stu08_checkpoint_and_journal_hold_no_answers(session):
    journal = Mock()
    journal.enabled = lambda: True
    session.agent.conversation_journal = journal
    say(session, 'потренируй меня по конспекту: два вопроса')
    say(session, 'мой секретный ответ')
    say(session, 'сохрани место')
    assert not journal.record_turn.called
    path = st.checkpoint_path(session.agent.config.paths.root, current(session).path)
    data = json.loads(path.read_text(encoding='utf-8'))
    assert set(data) == {'v', 'source', 'version', 'language', 'count', 'section', 'options', 'order', 'position', 'statuses', 'saved_at'}
    assert 'секрет' not in path.read_text(encoding='utf-8')


def test_stu09_delete_removes_only_the_checkpoint(session):
    say(session, 'потренируй меня по конспекту: два вопроса')
    say(session, 'сохрани место')
    path = st.checkpoint_path(session.agent.config.paths.root, current(session).path)
    assert path.exists()
    deleted = say(session, 'удали сохранённую тренировку').text
    assert 'удалена' in deleted and not path.exists() and (session.docs / 'конспект.txt').exists()
    assert 'нет' in say(session, 'удали сохранённую тренировку').text.casefold()


@pytest.mark.parametrize('code,phrase,count', [
    ('ru', 'потренируй меня по конспект.txt: пять вопросов', 5), ('ru', 'задай мне 3 вопроса по конспект.txt', 3),
    ('en', 'quiz me on конспект.txt: 4 questions', 4), ('en', 'ask me 2 questions about конспект.txt', 2),
    ('de', 'trainiere mich mit конспект.txt: 3 Fragen', 3), ('fr', 'entraîne-moi sur конспект.txt', 5),
    ('es', 'hazme 2 preguntas sobre конспект.txt', 2), ('it', 'allenami su конспект.txt: 3 domande', 3),
    ('pt', 'faz-me 2 perguntas sobre конспект.txt', 2),
])
def test_stu10_start_in_seven_languages(code, phrase, count):
    request = st.parse_start(phrase, code)
    assert request is not None and request['file'] == 'конспект.txt' and request['count'] == count


@pytest.mark.parametrize('code', CODES)
def test_stu10_controls_and_texts_in_seven_languages(code):
    words = {'ru': ('подсказка', 'пропусти', 'пауза', 'отмени тренировку'), 'en': ('hint', 'skip', 'pause', 'cancel the training'),
             'de': ('tipp', 'überspringen', 'pause', 'brich das training ab'), 'fr': ('indice', 'passe', 'pause', "annule l'entraînement"),
             'es': ('pista', 'salta', 'pausa', 'cancela el entrenamiento'), 'it': ('suggerimento', 'salta', 'pausa', "annulla l'allenamento"),
             'pt': ('dica', 'salta', 'pausa', 'cancela o treino')}[code]
    assert [st.command(w, code) for w in words] == ['hint', 'skip', 'pause', 'cancel']
    for key in st._T:
        assert st._T[key][CODES.index(code)]


def test_stu11_unusable_or_unknown_source_gives_a_bounded_result(session):
    assert 'не нашёл подходящих' in say(session, 'потренируй меня по пусто.txt').text.casefold()
    assert 'Не нашёл' in say(session, 'потренируй меня по несуществующий.txt').text
    assert current(session) is None


def test_stu12_question_count_is_bounded_and_section_filters(session):
    request = st.parse_start('задай мне 50 вопросов по конспект.txt', 'ru')
    assert request['count'] == st.MAX_QUESTIONS
    chunks = [{'text': TEXT, 'location': 'lines:1-5'}]
    built = st.build('x.txt', chunks, None, 'ru', count=10)
    assert built.count <= 5
    assert st.build('x.txt', chunks, None, 'ru', section='Нет такого раздела') is None
    with_options = st.build('x.txt', chunks, None, 'ru', count=2, options=True)
    assert all(q.answer in q.options and len(q.options) >= 2 for q in with_options.questions)
