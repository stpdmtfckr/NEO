"""NEO 188 NEO-VOICE-001 / TZ-CONTINUOUS-DIALOGUE-LOCALIZED-STOP-01."""
import pytest

from jarvis.runtime.continuous_stop188 import ended_text, is_stop


@pytest.mark.parametrize('code,phrase', [
    ('ru', 'Стоп'), ('ru', 'стоп, нео'), ('ru', 'нео перестань слушать'), ('ru', 'заверши диалог!'),
    ('en', 'Stop.'), ('en', 'end the conversation'), ('de', 'Stopp'), ('de', 'Gespräch beenden'),
    ('fr', 'Arrête !'), ('fr', "arrête d'écouter"), ('es', 'Para'), ('es', 'deja de escuchar'),
    ('it', 'Basta'), ('it', 'smetti di ascoltare'), ('pt', 'Pare'), ('pt', 'para de ouvir'),
])
def test_vstop01_02_selected_language_forms(code, phrase):
    assert is_stop(phrase, code, assistant_words=['нео', 'neo'])


@pytest.mark.parametrize('code,phrase', [
    ('ru', 'стоп музыка'), ('ru', 'останови видео'), ('en', 'stop the video'), ('de', 'stopp die Musik'),
    ('pt', 'pare a música'), ('ru', 'pare'), ('en', 'basta'), ('ru', 'стоп-кран это песня'), ('es', 'para mañana'),
])
def test_vstop03_targets_and_foreign_words_are_not_session_control(code, phrase):
    assert not is_stop(phrase, code, assistant_words=['нео', 'neo'])


def test_vstop05_custom_phrases_are_kept():
    assert is_stop('отбой', 'ru', configured=['отбой'])
    assert not is_stop('отбой', 'ru')


@pytest.mark.parametrize('code', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_vstop08_reply_only_claims_the_listening_change(code):
    text = ended_text(code)
    assert text and not any(word in text.casefold() for word in ('музык', 'music', 'musik', 'musique', 'música', 'musica'))
