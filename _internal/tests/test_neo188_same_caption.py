"""NEO 188 NEO-DESKTOP-001 / TZ-DESKTOP-SAME-CAPTION-TARGET-CHOICE-01 (inert fixtures, no real clicks)."""
from types import SimpleNamespace

import pytest

from jarvis.runtime import screen_targets171 as targets
from jarvis.runtime import ui_elements168 as elements
from jarvis.runtime.screen_targets171 import Hit, Window
from jarvis.tools import ui_control168

WINDOW = Window(7, 'Настройки', 'app.exe', 1, (0, 0, 900, 600), z=0)


def hit(rect, name='OK', method='uia', element=True):
    return Hit(WINDOW, name, rect, method, 1.0, element=SimpleNamespace(name=name, control_type='Button') if element else None)


def test_dtac01_two_same_caption_buttons_ask_instead_of_clicking_the_first():
    decision = targets.decide(targets.Search(hits=[hit((10, 10, 80, 40)), hit((800, 550, 880, 590))]))
    assert decision.kind == 'ask_element' and len(decision.options) == 2


def test_dtac02_wrappers_of_one_element_are_not_fake_options():
    decision = targets.decide(targets.Search(hits=[hit((10, 10, 80, 40)), hit((11, 11, 79, 39))]))
    assert decision.kind == 'click'


def test_dtac03_single_target_keeps_the_fast_path():
    assert targets.decide(targets.Search(hits=[hit((10, 10, 80, 40))])).kind == 'click'


def test_dtac04_choice_is_numbered_with_positions_and_bound_to_the_exact_elements(monkeypatch):
    shown = []
    monkeypatch.setattr(ui_control168, '_code', lambda config: 'ru')
    import jarvis.ui.desktop_events as events
    monkeypatch.setattr(events, 'request_desktop', lambda kind, **kw: shown.append((kind, kw)))
    first, second = hit((10, 10, 80, 40)), hit((800, 550, 880, 590))
    result = ui_control168._ask_element188(SimpleNamespace(get=lambda k, d=None: d), 'OK', [first, second])
    assert not result.success and result.data['requires_clarification'] and result.data['hinted']
    assert '1. вверху слева' in result.text and '2. внизу справа' in result.text and 'нажми 2' in result.text
    hwnd, element = elements.hinted(2)
    assert hwnd == 7 and element is second.element                 # «нажми 2» presses exactly this one
    assert shown and [m['n'] for m in shown[0][1]['marks']] == [1, 2]
    elements.forget()


def test_dtac05_ocr_only_duplicates_are_never_clicked_blindly(monkeypatch):
    monkeypatch.setattr(ui_control168, '_code', lambda config: 'ru')
    result = ui_control168._ask_element188(SimpleNamespace(get=lambda k, d=None: d), 'OK',
                                           [hit((10, 10, 80, 40), method='ocr', element=False), hit((800, 550, 880, 590), method='ocr', element=False)])
    assert not result.success and 'Ничего не нажимаю' in result.text and not result.data['hinted']


def test_dtac06_find_command_does_not_claim_one_found_element(monkeypatch):
    monkeypatch.setattr(ui_control168, '_code', lambda config: 'en')
    decision = targets.Decision('ask_element', options=[hit((10, 10, 80, 40)), hit((800, 550, 880, 590))])
    monkeypatch.setattr(ui_control168, '_search171', lambda config, name, window=0: ('OK', targets.Scope(), targets.Search(), decision))
    import jarvis.ui.desktop_events as events
    monkeypatch.setattr(events, 'request_desktop', lambda kind, **kw: None)
    result = ui_control168.find_by_name(SimpleNamespace(get=lambda k, d=None: d), 'OK')
    assert not result.success and 'several “OK”' in result.text
    elements.forget()


@pytest.mark.parametrize('code', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_dtac08_texts_in_seven_languages(code):
    text = targets.t('same_caption', code, title='T', name='OK', options='1. x\n2. y')
    assert 'OK' in text and '2' in text
