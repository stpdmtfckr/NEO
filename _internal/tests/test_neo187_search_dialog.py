"""NEO 187 REC-SEARCH-DIALOG-SOURCE-01: missing subject forms and non-answers."""
import pytest

from jarvis.runtime import search_intent186 as si


@pytest.mark.parametrize('code,text', [
    ('ru', 'Как это сделать?'), ('en', 'How do I do that?'), ('de', 'Wie mache ich das?'),
    ('fr', 'Comment est-ce que je fais ça ?'), ('es', '¿Cómo lo hago?'), ('it', 'Come lo faccio?'),
    ('pt', 'Como é que eu faço isso?'),
])
def test_open_how_to_without_subject_asks_instead_of_searching(code, text):
    assert si.decide(text, code).route == 'clarify_topic'


@pytest.mark.parametrize('code,text', [
    ('ru', 'Как сложить бумажного журавлика?'), ('en', 'How do I fold a paper crane?'),
    ('de', 'Wie faltet man einen Papierkranich?'),
])
def test_a_real_subject_still_searches(code, text):
    assert si.decide(text, code).route == 'search'


@pytest.mark.parametrize('text', ['Не знаю', 'да', 'I don’t know', 'maybe', 'Weiß nicht', 'je ne sais pas',
                                  'no sé', 'non lo so', 'não sei'])
def test_non_answers_are_recognized(text):
    assert si.is_non_answer(text)
    assert not si.is_non_answer('бумажный журавлик')
