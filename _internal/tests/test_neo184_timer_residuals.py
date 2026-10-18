"""N34/NB43/NB44: original user entrypoints, actual timer manager and Qt HUD."""
import copy
import os
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests.test_neo184_settings_mainwindow import app, window, settle
from jarvis.runtime import delayed_actions184 as delayed
from jarvis.runtime import timers
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution


@pytest.fixture(autouse=True)
def no_effects():
    with external_io_blocked(), patch.object(os,'system',side_effect=AssertionError('No shell')), patch.object(socket.socket,'sendto',side_effect=AssertionError('No UDP')):
        yield


@pytest.fixture
def real_timers(window,tmp_path,monkeypatch):
    manager=timers.TimerManager()
    monkeypatch.setattr(manager,'_ensure_thread_locked',lambda:None)
    manager.configure(window.config.paths.root,'ru')
    monkeypatch.setattr(timers,'_MANAGER',manager)
    window._timer_manager=manager
    window.config.data['user_name_enabled']=False
    window.config.data['ui']['timer_overlay_enabled']=True
    calls=[]
    for definition in window.agent.tools._tools.values():
        definition.handler=lambda args:ToolExecution(False,'QA inert')
    window.agent.tools.set_execution_guard(lambda name,args,risk:calls.append((name,copy.deepcopy(args))) or 'QA blocks external effects')
    monkeypatch.setattr(window.agent,'_refresh_foreground_context',lambda:{})
    monkeypatch.setattr(window.agent,'_active_game_id',lambda:'')
    monkeypatch.setattr(window.agent,'_capture_action_screen',lambda *_:None)
    monkeypatch.setattr(delayed,'capture_target',lambda action:{} if action=='shutdown' else {'label':'Original target'})
    monkeypatch.setattr(delayed,'bound_video_tab',lambda _:object())
    monkeypatch.setattr(delayed,'resolve_target',lambda _:object())
    return manager,calls


# Acceptance first: literal user wording through public Agent, real manager and HUD.
@pytest.mark.parametrize('phrase,action',[
 ('выключи комп через10минут','shutdown'),
 ('останови видео через10минут','stop_video'),
 ('выключи браузер через10минут','close_browser'),
])
def test_exact_user_examples_agent_timer_hud(window,real_timers,phrase,action):
    manager,calls=real_timers
    import time
    clock=SimpleNamespace(wall=time.time(),mono=time.monotonic())
    # Freeze only inside this entrypoint assertion, then restore before Qt
    # fixture cleanup. Scheduling/layout speed must not change the expectation.
    with patch.object(timers.time,'time',lambda:clock.wall), patch.object(timers.time,'monotonic',lambda:clock.mono):
        turn=window.agent.handle(phrase)
        if action=='shutdown':
            assert turn.pending and manager.list()==[]
            turn=window.agent.confirm_pending(True)
        assert turn.success and turn.outcome=='scheduled'
        item,=manager.list()
        assert item.action_spec['action']==action
        assert item.due_at==clock.wall+600 and manager.remaining(item)==600
        window._refresh_timer_stage_label()
        assert delayed.action_name(action,'ru') in window.timer_stage_label.text()
        assert '10:00' in window.timer_stage_label.text()
        assert window.timer_list_button184.isVisible() and calls==[]


BARE={
 'ru':'останови через10минут', 'en':'stop in10minutes', 'de':'stoppe in10Minuten',
 'fr':'arrête dans10minutes', 'es':'para en10minutos', 'it':'ferma tra10minuti', 'pt':'para em10minutos',
}
BROWSER_ONLY={
 'ru':'останови видео в Firefox', 'en':'stop video in Firefox', 'de':'stoppe das Video in Firefox',
 'fr':'arrête la vidéo dans Firefox', 'es':'para el vídeo en Firefox', 'it':'ferma il video in Firefox', 'pt':'para o vídeo em Firefox',
}

@pytest.mark.parametrize('code',BARE)
def test_ambiguous_delayed_target_asks_without_capture_or_schedule(window,real_timers,monkeypatch,code):
    manager,calls=real_timers
    window.config.data['app']['language']=code
    monkeypatch.setattr(delayed,'capture_target',lambda *_:(_ for _ in ()).throw(AssertionError('ambiguous target must never be probed')))
    turn=window.agent.handle(BARE[code])
    assert turn.outcome=='clarification' and turn.success is False and not turn.pending
    assert turn.text and not manager.list() and calls==[]

@pytest.mark.parametrize('code',BROWSER_ONLY)
def test_browser_preposition_without_duration_does_not_become_delayed(code):
    assert delayed.parse(BROWSER_ONLY[code],code) is None


def test_original_firefox_command_returns_to_existing_agent_route(window,real_timers):
    manager,calls=real_timers
    window.config.data['app']['language']='en'
    turn=window.agent.handle('stop video in Firefox')
    assert not manager.list() and turn.outcome!='scheduled'
    assert 'Use a delay' not in turn.text
    assert calls and calls[0][0]=='media' and calls[0][1].get('action')=='pause'

@pytest.mark.parametrize('phrase', ['не выключай комп через10минут', 'что означает команда «выключи комп через10минут»?', 'в документе написано: «выключи комп через10минут»', 'напомни через10минут выключить комп'])
def test_quotes_negation_and_reminder_do_not_schedule_power(window,real_timers,phrase):
    manager,calls=real_timers
    turn=window.agent.handle(phrase)
    assert not turn.pending
    assert not any(item.kind=='action' for item in manager.list())
    assert not any(name in {'system','media','window'} for name,_args in calls)


def test_spoken_cancellation_binds_exact_timer_id(window,real_timers):
    manager,calls=real_timers
    assert window.agent.handle('останови видео через10минут',input_source='voice').success
    selected=manager.list()[0]
    ordinary=manager.create(600,'Чай')
    assert window.agent.handle('отмени действие '+selected.id,input_source='voice').success
    assert [item.id for item in manager.list()]==[ordinary.id] and calls==[]


def test_cancelled_preview_and_new_ambiguous_request_cannot_authorize_shutdown(window,real_timers):
    manager,calls=real_timers
    assert window.agent.handle('выключи комп через10минут').pending
    assert window.agent.confirm_pending(False).outcome=='cancelled'
    assert not manager.list()
    assert window.agent.handle('выключи комп через10минут').pending
    assert window.agent.handle('останови через10минут').outcome=='clarification'
    assert not window.agent._pending_context
    window.agent.confirm_pending(True)
    assert not manager.list() and calls==[]


def test_actual_settings_apply_font_8_through_48_and_style_keys(window,real_timers,app):
    from PySide6.QtCore import QTimer
    manager,_=real_timers
    manager.create(600,'Чай')
    for font in (8,24,32,40,48):
        for style,radius,border in [('glass',7,1),('minimal',2,0),('pill',15,1)]:
            window.config.data['ui'].update(timer_overlay_font_size=font,timer_overlay_style=style,
                timer_overlay_text_color='#112233',timer_overlay_background_color='#AA3322',
                timer_overlay_border_color='#44CC88',timer_overlay_opacity_percent=63)
            with patch.object(QTimer,'singleShot',lambda *_a,**_k:None):
                window._apply_settings()
            label=window.timer_stage_label
            assert label.font().pixelSize()==font
            css=label.styleSheet()
            assert 'color:#112233' in css and 'background:rgba(170, 51, 34, 161)' in css
            assert f'border:{border}px solid #44CC88' in css and f'border-radius:{radius}px' in css
            assert 'padding:0px' not in css and 'background:transparent' not in css


def test_background_border_and_opacity_change_rendered_pixels(window,real_timers,app):
    from PySide6.QtCore import QTimer
    manager,_=real_timers
    manager.create(600,'Чай')
    window.config.data['ui'].update(timer_overlay_font_size=24,timer_overlay_style='glass',
        timer_overlay_background_color='#E02030',timer_overlay_border_color='#30D060',timer_overlay_opacity_percent=100)
    with patch.object(QTimer,'singleShot',lambda *_a,**_k:None):window._apply_settings()
    label=window.timer_stage_label
    settle(app)
    image=label.grab().toImage()
    point=(image.width()//2,image.height()-3)
    opaque=image.pixelColor(*point)
    assert opaque.name().upper()=='#E02030'
    assert image.pixelColor(image.width()//2,0).name().upper()=='#30D060'
    window.config.data['ui']['timer_overlay_opacity_percent']=25
    with patch.object(QTimer,'singleShot',lambda *_a,**_k:None):window._apply_settings()
    translucent=label.grab().toImage().pixelColor(*point)
    assert translucent!=opaque

@pytest.mark.parametrize('code',BARE)
def test_narrow_hud_large_font_list_click_live_countdown_and_exact_cancel(window,real_timers,app,code):
    manager,_=real_timers
    window.config.data['app']['language']=code
    items=[manager.create(600+i*10,f'Timer {i} with a long descriptive name') for i in range(7)]
    font=window.timer_stage_label.font();font.setPixelSize(48);window.timer_stage_label.setFont(font)
    window.robot.setFixedSize(160,90)
    window._refresh_timer_stage_label()
    button=window.timer_list_button184
    assert button.isVisible() and window.robot.rect().contains(button.geometry())
    assert window.timer_stage_label.font().pixelSize()==48
    QTest.mouseClick(button,Qt.LeftButton)
    settle(app)
    dialog=button.dialog
    assert dialog is not None and dialog.isVisible() and dialog.rows.count()==7
    assert all('Timer' in dialog.rows.item(i).text() and '[' in dialog.rows.item(i).text() for i in range(7))
    selected=dialog.rows.item(4);selected_id=selected.data(Qt.UserRole)
    dialog.rows.setCurrentItem(selected)
    manager.cancel(items[0].id)
    window._refresh_timer_stage_label()
    assert dialog.rows.currentItem().data(Qt.UserRole)==selected_id
    manager.reschedule(selected_id,120)
    window._refresh_timer_stage_label()
    assert '2:00' in dialog.rows.currentItem().text()
    QTest.mouseClick(dialog.cancel_button,Qt.LeftButton)
    assert selected_id not in {item.id for item in manager.list()}
    assert len(manager.list())==5
    dialog.reject()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
    assert button.dialog is None


def test_list_cancellation_failure_keeps_target_and_other_timers(window,real_timers,app,monkeypatch):
    manager,_=real_timers
    items=[manager.create(600,name) for name in ('First','Second')]
    window._refresh_timer_stage_label()
    QTest.mouseClick(window.timer_list_button184,Qt.LeftButton);settle(app)
    dialog=window.timer_list_button184.dialog
    dialog.rows.setCurrentRow(1)
    monkeypatch.setattr(manager,'_save_locked',lambda:(_ for _ in ()).throw(OSError('Disk full')))
    QTest.mouseClick(dialog.cancel_button,Qt.LeftButton)
    assert {item.id for item in manager.list()}=={item.id for item in items}
    assert 'остаётся активным' in dialog.notice.text()
    dialog.reject()


def test_new_ambiguous_request_clears_previous_duration_slot(window,real_timers):
    manager,calls=real_timers
    window.agent.handle('поставь таймер')
    state=window.agent.conversations.get('desktop')
    assert state.pending_intent is not None
    assert window.agent.handle('останови через10минут').outcome=='clarification'
    assert state.pending_intent is None and not manager.list() and calls==[]


def test_spoken_cancel_discards_shutdown_preview(window,real_timers):
    manager,calls=real_timers
    assert window.agent.handle('выключи комп через10минут',input_source='voice').pending
    window.agent.handle('отмени',input_source='voice')
    assert not window.agent._pending_context
    window.agent.confirm_pending(True)
    assert not manager.list() and calls==[]


def test_original_entrypoint_hud_survives_clock_jump_and_restart_cancels(window,real_timers,monkeypatch):
    manager,calls=real_timers
    import time
    clock=SimpleNamespace(wall=time.time(),mono=time.monotonic())
    with monkeypatch.context() as m:
        m.setattr(timers.time,'time',lambda:clock.wall)
        m.setattr(timers.time,'monotonic',lambda:clock.mono)
        assert window.agent.handle('останови видео через10минут').success
        original=manager.list()[0]
        clock.wall+=86400
        window._refresh_timer_stage_label()
        assert '10:00' in window.timer_stage_label.text()
        clock.wall-=172800
        window._refresh_timer_stage_label()
        assert '10:00' in window.timer_stage_label.text()
        restarted=timers.TimerManager();m.setattr(restarted,'_ensure_thread_locked',lambda:None)
        restarted.configure(window.config.paths.root,'ru')
        window._timer_manager=restarted
        window._refresh_timer_stage_label()
        assert not restarted.list() and not window.timer_list_button184.isVisible()
        assert any(original.label in event.text for event in restarted.drain())
    assert calls==[]


def test_original_entrypoint_save_failure_never_shows_scheduled_hud(window,real_timers,monkeypatch):
    manager,calls=real_timers
    monkeypatch.setattr(manager,'_save_locked',lambda:(_ for _ in ()).throw(OSError('Disk full')))
    turn=window.agent.handle('выключи комп через10минут')
    assert turn.pending
    result=window.agent.confirm_pending(True)
    window._refresh_timer_stage_label()
    assert result.success is False and not manager.list()
    assert not window.timer_stage_label.isVisible() and not window.timer_list_button184.isVisible()
    assert calls==[]


SPOKEN_INCOMPLETE={
 'ru':('останови видео через ','десять','двадцать пять','минус десять','одну','две тысячи','тысячу','непонятная длительность'),
 'en':('stop video in ','ten','twenty five','minus ten','one','one thousand','hundred','an unknown duration'),
 'de':('stoppe das Video in ','zehn','fünfundzwanzig','minus zehn','eine','zwei hundert','tausend','unbekannte dauer'),
 'fr':('arrête la vidéo dans ','dix','vingt-cinq','moins dix','une','deux mille','mille','durée inconnue'),
 'es':('para el vídeo en ','diez','veinticinco','menos diez','una','dos mil','mil','duración desconocida'),
 'it':('ferma il video tra ','dieci','venticinque','meno dieci','una','due mila','mille','durata sconosciuta'),
 'pt':('para o vídeo em ','dez','vinte e cinco','menos dez','uma','dois mil','mil','duração desconhecida'),
}

@pytest.mark.parametrize('code',SPOKEN_INCOMPLETE)
def test_public_agent_spoken_missing_units_never_attempts_immediate_media(window,real_timers,code):
    manager,calls=real_timers
    window.config.data['app']['language']=code
    prefix,*numbers=SPOKEN_INCOMPLETE[code]
    for number in numbers:
        phrase=prefix+number
        turn=window.agent.handle(phrase)
        assert turn.success is False and turn.outcome=='clarification', (phrase,turn)
        assert not turn.pending and not manager.list() and calls==[], (phrase,calls)
        assert turn.text
