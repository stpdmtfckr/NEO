"""NEO 172: «что за ошибка?» / «как это исправить?» / «как пройти этот уровень?» and «как сделать …»."""
from __future__ import annotations

import pytest

from jarvis.game_web.models import SearchResponse, SearchResult
from jarvis.runtime import screen_help172 as help_
from jarvis.runtime import web_answers171
from jarvis.runtime.screen_targets171 import Window


@pytest.fixture(autouse=True)
def _clean():
    help_.forget()
    web_answers171.forget()
    yield
    help_.forget()
    web_answers171.forget()


@pytest.mark.parametrize('text', [
    'что за ошибка', 'что это за ошибка', 'что за ошибка на экране', 'как это исправить', 'как исправить эту ошибку', 'помоги с этой ошибкой',
    'почему это не работает', 'как пройти этот уровень', 'что тут не так',
    'what is this error', "what's this error", 'how do i fix this', 'help me with this', 'how do i beat this level',
    'was ist das für ein fehler', 'wie behebe ich das', "c'est quoi cette erreur", 'qué es este error', 'cómo arreglo esto',
    'che errore è questo', 'come risolvo questo', 'que erro é este', 'como corrijo isto',
])
def test_questions_about_the_screen(text):
    assert help_.asks_about_screen(text)


@pytest.mark.parametrize('text', ['как установить драйвер на видеокарту', 'открой браузер', 'что такое рекурсия', 'ошибка 404 что значит в http'])
def test_not_about_the_screen(text):
    assert not help_.asks_about_screen(text)


def test_problem_lines_are_the_error_lines_in_screen_order():
    lines = ['Steam', 'Файл', 'Не удалось подключиться к сети Steam', 'Код ошибки: 118', 'OK', 'Отмена', 'Подробнее']
    assert help_.pick_lines(lines) == ['Не удалось подключиться к сети Steam', 'Код ошибки: 118']
    assert help_.pick_lines(['Главное меню', 'Нажмите любую клавишу чтобы продолжить игру', 'OK']) == ['Нажмите любую клавишу чтобы продолжить игру']
    assert help_.pick_lines(['OK', 'Отмена']) == []


def test_personal_data_never_goes_to_the_search():
    text = help_.scrub(r'Не найден C:\Users\Vladimir\AppData\game.dll, пишите на vova@mail.ru, карта 4276 1234 5678 9012, ключ ABCDEFGHIJKLMNOPQRSTUVWXYZ012345, 192.168.1.10')
    for secret in ('Vladimir', 'vova@mail.ru', '4276', 'ABCDEFGHIJ', '192.168'):
        assert secret not in text
    assert r'C:\Users\…\AppData\game.dll' in text


def test_query_is_program_problem_and_how_to_fix():
    reading = help_.Reading('Steam', 'Steam', ['Не удалось подключиться к сети Steam', 'Код ошибки: 118'], 7, 'ocr')
    assert help_.build_query(reading, 'что за ошибка', 'ru') == 'Не удалось подключиться к сети Steam Код ошибки: 118 как исправить'
    game = help_.Reading('Hollow Knight', 'Hollow Knight', ['Hornet Sentinel'], 3, 'ocr', game=True)
    assert help_.build_query(game, 'как пройти этот уровень', 'ru') == 'Hollow Knight Hornet Sentinel как пройти'
    assert help_.build_query(reading, 'what is this error', 'en').endswith('how to fix')


class FakeImage:
    def __init__(self):
        self.saved = []

    def save(self, path):
        self.saved.append(path)
        open(path, 'wb').write(b'png')


class FakeVision:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def analyze_image(self, path, prompt, **kw):
        self.calls.append(kw)
        return self.answer


def _window(game=False):
    return Window(5, 'Ошибка — Steam', 'steam.exe', 55, (100, 100, 700, 500), monitor=1, z=0, game=game)


def test_read_screen_uses_ocr_first_and_the_screen_model_only_when_ocr_found_nothing():
    vision = FakeVision('Steam error code 118')
    reading = help_.read_screen(ollama=vision, window=_window(), grab=lambda *a: FakeImage(),
                                lines_of=lambda image: ['Не удалось подключиться', 'OK'])
    assert reading.method == 'ocr' and reading.lines == ['Не удалось подключиться'] and not vision.calls
    assert reading.app == 'Steam'
    image = FakeImage()
    reading = help_.read_screen(ollama=vision, window=_window(), grab=lambda *a: image, lines_of=lambda image: [])
    assert reading.method == 'vision' and reading.lines == ['Steam error code 118']
    assert vision.calls == [{'num_predict': 60, 'keep_alive': 0}]          # the screen model is unloaded right away
    import os
    assert image.saved and not os.path.exists(image.saved[0])               # the temporary screenshot is deleted
    none = help_.read_screen(ollama=FakeVision('NONE'), window=_window(), grab=lambda *a: FakeImage(), lines_of=lambda image: [])
    assert none.method == 'none' and none.lines == []


class _Searcher:
    def __init__(self):
        self.queries = []

    def search(self, query, *, language='ru', **_):
        self.queries.append(query)
        answer = ('Ошибка 118 означает, что Steam не может подключиться к серверам.\n\n'
                  '1. Перезапустите роутер.\n2. Разрешите Steam в брандмауэре Windows.\n3. Отключите VPN и прокси.\n'
                  '4. Очистите кэш загрузки в настройках Steam.\n\nЕсли не помогло, обратитесь в поддержку.')
        return SearchResponse(query, 'searchapi_google', [SearchResult('help', 'https://help.steampowered.com')], answer)

    def close(self):
        pass


def _session(tmp_path, reading):
    from jarvis.testing.atlas_session import AtlasSession
    session = AtlasSession(tmp_path / 'run')
    session.reset_case('ru')
    session.agent.game_web.searcher = searcher = _Searcher()
    session.agent.game_web.cache.get = lambda *a, **k: None
    session.agent._screen_reader172 = lambda **kw: reading
    return session, searcher


def test_whole_dialogue_asks_before_sending_and_answers_in_steps(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    reading = help_.Reading('Steam', 'Steam', ['Не удалось подключиться к сети Steam', r'Код ошибки: 118 (C:\Users\Vova\steam.log)'], 9, 'ocr')
    with external_io_blocked():
        session, searcher = _session(tmp_path, reading)
        try:
            first = session.agent.handle('что за ошибка')
            assert searcher.queries == []                                   # nothing sent yet
            assert first.text.startswith('Вижу в окне «Steam»: Не удалось подключиться к сети Steam')
            assert 'Отправлю в поиск: «Не удалось подключиться к сети Steam Код ошибки: 118 (steam.log) как исправить»' in first.text
            assert 'Vova' not in first.text and 'снимок экрана не отправляется' in first.text
            second = session.agent.handle('да')
            assert len(searcher.queries) == 1 and 'Vova' not in searcher.queries[0]
            assert 'Что сделать:\n1. Перезапустите роутер.' in second.text and second.tool_details == ['help.search']
            assert 'поддержку' not in second.text.split('Что сделать:')[1]
        finally:
            session.finish()


def test_no_means_nothing_is_sent_and_a_new_request_drops_the_question(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    reading = help_.Reading('Steam', 'Steam', ['Код ошибки: 118'], 4, 'ocr')
    with external_io_blocked():
        session, searcher = _session(tmp_path, reading)
        try:
            session.agent.handle('что за ошибка')
            assert session.agent.handle('нет').text == 'Хорошо, ничего не отправляю.'
            session.agent.handle('что за ошибка')
            session.agent.handle('открой калькулятор')
            assert not help_.pending() and searcher.queries == []
        finally:
            session.finish()


def test_without_confirmation_setting_it_searches_at_once(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    reading = help_.Reading('Steam', 'Steam', ['Код ошибки: 118'], 4, 'ocr')
    with external_io_blocked():
        session, searcher = _session(tmp_path, reading)
        try:
            session.agent.config.data.setdefault('help', {})['confirm_before_search'] = False
            session.agent.handle('что за ошибка')
            assert searcher.queries == ['Steam Код ошибки: 118 как исправить']
        finally:
            session.finish()


def test_nothing_readable_asks_what_is_wrong(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session, searcher = _session(tmp_path, help_.Reading('Блокнот', 'Без имени — Блокнот', [], 0, 'none'))
        try:
            turn = session.agent.handle('что за ошибка')
            assert turn.text.startswith('Не нашёл на экране текста ошибки в окне «Блокнот»') and searcher.queries == []
        finally:
            session.finish()


@pytest.mark.parametrize('question', ['как установить драйвер на видеокарту', 'как пройти 5 уровень в cuphead', 'how do i reinstall windows',
                                      'wie installiere ich python', 'comment installer python', 'cómo instalar python', 'come installare python',
                                      'como instalar python'])
def test_how_to_questions_go_to_the_web(question):
    assert web_answers171.classify(question) == 'web' and 'howto' in web_answers171.facets(question)


def test_how_to_answer_keeps_the_steps_in_order():
    answer = ('Чтобы установить драйвер NVIDIA, выполните шаги.\n\n1. Откройте nvidia.com.\n2. Выберите модель видеокарты.\n'
              '3. Скачайте установщик.\n4. Отметьте «Чистая установка».\n5. Перезагрузите компьютер.\n\nТакже можно обновить через NVIDIA App.')
    body, shortened = web_answers171.structure('как установить драйвер на видеокарту', answer, 'ru')
    assert body.splitlines()[0] == 'Чтобы установить драйвер NVIDIA, выполните шаги.'
    assert 'Что сделать:\n1. Откройте nvidia.com.\n2. Выберите модель видеокарты.\n3. Скачайте установщик.\n4. Отметьте «Чистая установка».\n5. Перезагрузите компьютер.' in body
    assert shortened        # the extra sentence stays available by «подробнее»
