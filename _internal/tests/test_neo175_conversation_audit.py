"""NEO 175: conversation is never turned into an action or a service report.

Regression: «Привет, как дела?» was split into two «steps» («привет» + «как дела»),
sent to the agent model as a task, and the model's invented command printed the
statistics of the NEO command model. Here the agent model and the command model
deliberately invent service commands for every phrase: no conversational phrase
may run anything.
"""
import pytest

from jarvis.runtime import agent_llm170, neo_model169
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

CONVERSATION = {
    'ru': [
        'привет', 'Привет, как дела?', 'привет как дела', 'привет, как дела', 'Привет! Как дела?', 'здравствуй, как ты?',
        'как дела', 'как ты', 'как твои дела', 'как у тебя дела?', 'что нового', 'привет, что нового', 'доброе утро',
        'доброе утро, как спалось?', 'добрый вечер', 'добрый вечер, как прошёл день?', 'спасибо', 'спасибо, ты лучший',
        'спасибо большое', 'пока', 'до свидания', 'спокойной ночи', 'мне скучно', 'мне грустно', 'я устал',
        'расскажи анекдот', 'пошути', 'кто ты', 'как тебя зовут', 'что ты умеешь', 'ты молодец', 'ты классный',
        'давай поболтаем', 'как прошёл твой день', 'ну привет', 'хай', 'ладно', 'понятно', 'хорошо, спасибо',
        'привет, ты тут?', 'привет, рад тебя слышать', 'как настроение?', 'привет, как настроение',
        # with typos and colloquial forms
        'привет как дила', 'превет, как дела', 'спосибо', 'пасиба', 'добрый вечир', 'мне скушно', 'как настраение',
        'как жизнь', 'что делаешь', 'чем занимаешься', 'ты лучший', 'ты умница', 'здарова', 'ок спасибо',
    ],
    'en': [
        'hi', 'hello, how are you?', 'hey, how are you doing', 'good morning', 'good morning, how did you sleep?',
        'thanks', 'thank you so much', 'bye', 'who are you', 'what can you do', 'tell me a joke', "i'm bored",
        "i'm tired", 'how are you', 'hello there',
        'whats up', "what's up", "you're awesome", 'you are awesome', 'good job', 'well done', 'helo, how are you',
        'how r u', 'thx', 'nice to meet you', "how's it going",
    ],
    'de': ['hallo, wie geht es dir?', 'guten morgen', 'danke', 'wie gehts', 'was machst du'],
    'fr': ['salut, ça va ?', 'bonjour, comment ça va ?', 'merci', 'salut ca va'],
    'es': ['hola, ¿qué tal?', 'hola, ¿cómo estás?', 'gracias', 'hola que tal', 'qué haces'],
    'it': ['ciao, come stai?', 'buongiorno', 'grazie', 'ciao come stai', 'che fai'],
    'pt': ['olá, tudo bem?', 'olá, como estás?', 'obrigado', 'ola tudo bem'],
}
REPORT_WORDS = ('Модель команд NEO', 'command model', 'Точность', 'Accuracy', 'Поправок', 'дообуч')


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('audit175') / 'run')
        yield value
        value.finish()


@pytest.fixture
def inventive_models(monkeypatch):
    """Both models «hallucinate» a NEO service command for whatever they see."""
    seen = []

    def decide(agent, text, code, on_reply=None):
        seen.append(text)
        return agent_llm170.Decision('do', commands=['статус модели команд' if code == 'ru' else 'command model status'])
    monkeypatch.setattr(agent_llm170, 'decide', decide)
    monkeypatch.setattr(neo_model169, 'propose', lambda agent, text, code: neo_model169.Proposal(
        'neo_model.status', 'neo_model', {'action': 'status'}, 0.99))
    return seen


CASES = [(code, phrase) for code, phrases in CONVERSATION.items() for phrase in phrases]


@pytest.mark.parametrize('code,phrase', CASES)
def test_conversation_never_runs_a_command(session, inventive_models, code, phrase):
    session.reset_case(code)
    turn = session.agent.handle(phrase)
    assert not session.calls, (phrase, session.calls, turn.tool_details)
    assert not any(word.casefold() in str(turn.text).casefold() for word in REPORT_WORDS), (phrase, turn.text)


def test_greeting_with_a_question_is_one_utterance_not_two_steps(session):
    from jarvis.runtime import tasks169
    verbs = session.agent._step_verbs170('ru')
    for phrase in ('Привет, как дела?', 'привет, что нового', 'привет, где ты был', 'спасибо, мне хорошо'):
        assert len(tasks169.split_soft(phrase, 'ru', verbs)) == 1, phrase
    assert not {'как', 'что', 'где', 'когда', 'мне', 'хочу'} & verbs
    # Real multi-step requests still split.
    assert len(tasks169.split_soft('открой браузер, найди рецепт и перейди на сайт с отзывами', 'ru', verbs)) >= 2


def test_service_tools_still_work_when_asked(session):
    session.reset_case('ru')
    session.agent.handle('статус модели команд')
    assert session.calls and session.calls[0]['tool'] == 'neo_model'


def test_invented_service_command_for_a_real_request_is_not_run(session, monkeypatch):
    monkeypatch.setattr(agent_llm170, 'decide', lambda agent, text, code, on_reply=None:
                        agent_llm170.Decision('do', commands=['статус модели команд']))
    session.reset_case('ru')
    turn = session.agent.handle('сделай что-нибудь интересное с экраном')
    assert not any(call['tool'] == 'neo_model' for call in session.calls), turn.tool_details


@pytest.mark.parametrize('code,phrase', [('en', 'whats up'), ('en', "you're awesome"), ('ru', 'что делаешь'), ('ru', 'ты лучший')])
def test_small_talk_never_matches_a_command_by_similarity(session, code, phrase):
    """Without any model: «whats up» used to open Task Manager, «you're awesome» listed the memory."""
    session.reset_case(code)
    session.agent.handle(phrase)
    assert not session.calls, session.calls


@pytest.mark.parametrize('code,phrase,tool', [
    ('ru', 'больше ен включай текущую песню в яндекс музыке', 'yandex_music'),       # «ен» -> «не»: dislike, never play
    ('ru', 'слушай, больше не включай текущую песню в яндекс музыке', 'yandex_music'),
    ('ru', 'а теперь открой калькулятор', 'application'),
    ('ru', 'калькулятор открой', 'application'),
    ('en', 'hey, open calculator', 'application'),
])
def test_phrasing_and_short_word_typos_keep_the_command(session, code, phrase, tool):
    session.reset_case(code)
    session.agent.handle(phrase)
    assert session.calls and session.calls[0]['tool'] == tool, session.calls
    assert session.calls[0]['arguments'].get('action') not in {'play_track', 'play'}, session.calls


def test_weather_on_tomorrow_is_not_a_city(session):
    session.reset_case('ru')
    session.agent.handle('покажи погоду на завтра')
    assert session.calls and session.calls[0]['arguments'].get('city') == ''


def test_a_typo_never_sends_an_email_reply(session):
    """«отметь письмо прочитанным» with a typo was guessed as «ответь …» and sent at once."""
    session.reset_case('ru')
    session.agent.handle('отметь письмо тест прочитаннмы')
    assert not any(call['tool'] == 'email' and call['arguments'].get('action') in {'reply', 'send'} for call in session.calls)


def test_sending_a_message_always_asks_first(session):
    from jarvis.tools.registry import Risk
    agent = session.agent
    for tool, action in (('email', 'send'), ('email', 'reply'), ('telegram_messages', 'send'), ('telegram_messages', 'reply')):
        assert agent._needs_confirmation(tool, {'action': action}, Risk.MEDIUM), (tool, action)
    assert not agent._needs_confirmation('email', {'action': 'unread'}, Risk.SAFE)


def test_a_guess_never_turns_a_request_into_its_opposite(session):
    from jarvis.runtime.typos175 import opposite_guess
    assert opposite_guess('close', 'opn test in the mini browser')
    assert opposite_guess('open', 'clsoe the browser')
    assert not opposite_guess('close', 'close the browser') and not opposite_guess('pause', 'поставь на паузу')
    session.reset_case('en')
    session.agent.handle('opn test in the mini browser')
    assert not any(call['arguments'].get('action') == 'close' for call in session.calls)
