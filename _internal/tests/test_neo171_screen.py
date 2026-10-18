"""NEO 171: pressing a named thing in ANY window, on any monitor (the situation from the 168 log:
Task Manager — running as administrator — was the window under NEO, and the game was on screen too)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.runtime import screen_targets171 as targets
from jarvis.runtime.screen_targets171 import Hit, Monitor, Scope, Window

MONITORS = [Monitor(1, (0, 0, 1920, 1080), True), Monitor(2, (1920, 0, 3840, 1080), False)]


def _windows():
    return [
        Window(11, 'Диспетчер задач', 'taskmgr.exe', 100, (100, 100, 900, 800), monitor=1, z=0, protected=True),
        Window(22, 'Dota 2', 'dota2.exe', 200, (1920, 0, 3840, 1080), monitor=2, z=1, game=True),
        Window(33, 'Яндекс Музыка', 'yandexmusic.exe', 300, (200, 150, 1400, 900), monitor=1, z=2),
    ]


class Line:
    def __init__(self, words):
        self.words = [SimpleNamespace(text=t, x=x, y=10, width=w, height=20) for t, x, w in words]
        self.text = ' '.join(t for t, _, _ in words)


def _search(name, scope=Scope(), *, uia=None, ocr=None, restrict=0, listing=_windows):
    uia = uia or (lambda w, n: ([], 0 if w.game else 120))
    ocr = ocr or (lambda w, n: [])
    return targets.search(name, scope, restrict_hwnd=restrict, uia=uia, ocr=ocr, listing=listing, screens=lambda: MONITORS)


def test_admin_window_is_skipped_and_named_and_the_game_is_searched_by_ocr():
    looked = []

    def ocr(window, name):
        looked.append(window.process)
        return [Hit(window, 'Продолжить', (2500, 600, 2620, 640), 'ocr', 1.0)] if window.game else []

    found = _search('Продолжить', ocr=ocr)
    assert [w.process for w in found.protected] == ['taskmgr.exe']
    assert 'dota2.exe' in looked and 'taskmgr.exe' not in looked
    decision = targets.decide(found)
    assert decision.kind == 'click' and decision.hit.window.process == 'dota2.exe' and decision.hit.method == 'ocr'


def test_found_in_two_windows_asks_where_and_the_answer_works():
    def uia(window, name):
        if window.process == 'yandexmusic.exe':
            return [Hit(window, 'Играть', (300, 300, 380, 330), 'uia', 1.0, element=object())], 50
        return [], 0

    def ocr(window, name):
        return [Hit(window, 'Играть', (2600, 700, 2700, 740), 'ocr', 1.0)] if window.game else []

    found = _search('Играть', uia=uia, ocr=ocr)
    decision = targets.decide(found)
    assert decision.kind == 'ask_window' and {h.window.process for h in decision.options} == {'dota2.exe', 'yandexmusic.exe'}
    question = targets.ask('Играть', decision.options, 'ru')
    assert question.startswith('«Играть» есть в нескольких окнах') and 'Dota 2 — экран 2' in question
    assert targets.pending()
    assert targets.resolve('на втором мониторе', 'ru') == {'action': 'click_name', 'name': 'Играть', 'window': '22'}
    targets.ask('Играть', decision.options, 'ru')
    assert targets.resolve('в игре', 'ru')['window'] == '22'
    targets.ask('Играть', decision.options, 'ru')
    number = next(i for i, h in enumerate(decision.options, 1) if h.window.process == 'yandexmusic.exe')
    assert targets.resolve(str(number), 'ru')['window'] == '33'
    targets.ask('Играть', decision.options, 'ru')
    assert targets.resolve('яндекс', 'ru')['window'] == '33'
    targets.ask('Играть', decision.options, 'ru')
    assert targets.resolve('включи музыку', 'ru') is None      # not an answer: a new request
    targets.forget_pending()


def test_the_window_neo_just_worked_in_wins_instead_of_asking():
    hits = [Hit(w, 'OK', (0, 0, 10, 10), 'uia', 1.0) for w in _windows()[1:]]
    decision = targets.decide(targets.Search(hits=hits), last_hwnd=33)
    assert decision.kind == 'click' and decision.hit.window.hwnd == 33


def test_scope_narrows_the_windows():
    windows = _windows()
    assert [w.hwnd for w in targets.select(windows, Scope(monitor=2), MONITORS)] == [22]
    assert [w.hwnd for w in targets.select(windows, Scope(side='left'), MONITORS)] == [11, 33]
    assert [w.hwnd for w in targets.select(windows, Scope(game=True), MONITORS)] == [22]
    assert [w.hwnd for w in targets.select(windows, Scope(app='диспетчере задач'), MONITORS)] == [11]
    assert [w.hwnd for w in targets.select(windows, Scope(app='яндекс'), MONITORS)] == [33]


def test_nothing_found_explains_where_it_looked():
    from jarvis.tools import ui_control168
    config = SimpleNamespace(get=lambda k, d=None: 'ru' if k == 'app.language' else d)
    found = _search('Сохранить')
    result = ui_control168._not_found171(config, 'Сохранить', Scope(), found)
    assert not result.success
    assert 'Не нашёл «Сохранить» в окнах: «Dota 2» (2), «Яндекс Музыка» (1)' in result.text
    assert '«Диспетчер задач» запущен от имени администратора' in result.text
    assert 'на каком экране' in result.text                       # two monitors and no place given


def test_exact_hit_in_the_working_window_stops_the_search():
    calls = []

    def listing():
        return [Window(1, 'Chrome', 'chrome.exe', 1, (0, 0, 800, 600), z=0), Window(2, 'Word', 'winword.exe', 2, (0, 0, 800, 600), z=1)]

    def uia(window, name):
        calls.append(window.process)
        return [Hit(window, 'Отправить', (10, 10, 50, 30), 'uia', 1.0)], 80

    found = _search('Отправить', uia=uia, listing=listing)
    assert calls == ['chrome.exe'] and targets.decide(found).kind == 'click'


def test_ocr_hit_finds_multi_word_text_and_converts_to_screen_pixels():
    lines = [Line([('Главное', 0, 60), ('меню', 70, 40)]), Line([('Моя', 100, 40), ('волна', 150, 60), ('▶', 220, 10)])]
    rect, score, text = targets.ocr_hit(lines, 'моя волна', 0.5, (1000, 500))
    assert score == 1.0 and text == 'Моя волна'
    assert rect == (1000 + 200, 500 + 20, 1000 + 420, 500 + 60)     # scale 0.5 -> doubled, then shifted to the window
    assert targets.ocr_hit(lines, 'продолжить', 1.0, (0, 0)) is None
    assert targets.ocr_hit([Line([('Прoдолжить', 0, 90)])], 'Продолжить', 1.0, (0, 0))[1] >= 0.84   # Latin «o» from OCR


@pytest.mark.parametrize('text,code,name,scope', [
    ('Продолжить в игре', 'ru', 'Продолжить', Scope(game=True)),
    ('кнопку ОК на втором мониторе', 'ru', 'кнопку ОК', Scope(monitor=2)),
    ('Настройки в диспетчере задач', 'ru', 'Настройки', Scope(app='диспетчере задач')),
    ('OK on the left screen', 'en', 'OK', Scope(side='left')),
    ('Play in the game', 'en', 'Play', Scope(game=True)),
    ('Weiter auf dem zweiten Monitor', 'de', 'Weiter', Scope(monitor=2)),
    ('Continuer sur le deuxième écran', 'fr', 'Continuer', Scope(monitor=2)),
    ('Continuar en la segunda pantalla', 'es', 'Continuar', Scope(monitor=2)),
    ('Continua sul secondo schermo', 'it', 'Continua', Scope(monitor=2)),
    ('Continuar no segundo monitor', 'pt', 'Continuar', Scope(monitor=2)),
    ('Играть на основном мониторе', 'ru', 'Играть', Scope(primary=True)),
    ('моя волна', 'ru', 'моя волна', Scope()),
])
def test_place_phrases(text, code, name, scope):
    assert targets.parse_scope(text, code) == (name, scope)


@pytest.mark.parametrize('text,name', [
    ('а кнопку моя волна', 'моя волна'),                 # «на» heard as «а» (from the 168 log)
    ('на кнопку "моя волна', 'моя волна'),
    ('по надписи Настройки', 'Настройки'),
    ('на иконку корзины', 'корзины'),
])
def test_button_words_are_not_part_of_the_name(text, name):
    from jarvis.tools.ui_control168 import button_name
    assert button_name(text) == name


def test_click_by_name_presses_the_ocr_text_in_the_game(monkeypatch):
    from jarvis.runtime import input168
    from jarvis.tools import ui_control168
    clicks = []
    game = _windows()[1]
    monkeypatch.setattr(targets, 'search', lambda name, scope, restrict_hwnd=0: targets.Search(
        hits=[Hit(game, 'Продолжить', (2500, 600, 2620, 640), 'ocr', 1.0)], searched=[game], monitors=2))
    monkeypatch.setattr(targets, 'bring_to_front', lambda window: True)
    monkeypatch.setattr(targets, 'covered', lambda hit: False)
    monkeypatch.setattr(input168, 'click_at', lambda x, y, **kw: clicks.append((x, y)))
    config = SimpleNamespace(get=lambda k, d=None: 'ru' if k == 'app.language' else d)
    result = ui_control168.click_by_name(config, 'кнопку Продолжить в игре')
    assert result.success and clicks == [(2560, 620)] and 'в окне «Dota 2»' in result.text


def test_a_covered_target_is_not_clicked(monkeypatch):
    from jarvis.runtime import input168
    from jarvis.tools import ui_control168
    game = _windows()[1]
    monkeypatch.setattr(targets, 'search', lambda name, scope, restrict_hwnd=0: targets.Search(
        hits=[Hit(game, 'Продолжить', (2500, 600, 2620, 640), 'ocr', 1.0)], searched=[game]))
    monkeypatch.setattr(targets, 'bring_to_front', lambda window: False)
    monkeypatch.setattr(targets, 'covered', lambda hit: True)
    monkeypatch.setattr(input168, 'click_at', lambda *a, **k: pytest.fail('must not click'))
    config = SimpleNamespace(get=lambda k, d=None: 'ru' if k == 'app.language' else d)
    result = ui_control168.click_by_name(config, 'Продолжить')
    assert not result.success and 'закрыт другим окном' in result.text


def test_answer_to_where_goes_to_the_ui_tool_through_the_agent(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        try:
            session.reset_case('ru')
            targets.ask('Играть', [Hit(w, 'Играть', (0, 0, 1, 1), 'uia', 1.0) for w in _windows()[1:]], 'ru')
            session.calls.clear()
            session.agent.handle('второе')
            assert session.calls and session.calls[0]['tool'] == 'ui'
            assert session.calls[0]['arguments'] == {'action': 'click_name', 'name': 'Играть', 'window': '33'}
        finally:
            targets.forget_pending()
            session.finish()
