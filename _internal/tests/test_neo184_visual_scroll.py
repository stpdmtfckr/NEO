"""Live-edge following and reader preservation, with no model, mic or transport I/O."""
from __future__ import annotations
import os
import queue
import time
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from PySide6.QtWidgets import QApplication,QLineEdit
from PySide6.QtCore import QTimer,QCoreApplication,QEvent

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    for key in ('HOME','LOCALAPPDATA','APPDATA','XDG_DATA_HOME'):
        monkeypatch.setenv(key,str(tmp_path/key))
    from jarvis.testing.isolation import external_io_blocked
    import socket
    def deny(*a,**k): raise PermissionError('Visual tests forbid external I/O')
    with external_io_blocked(),patch.object(socket.socket,'sendto',deny),patch.object(os,'system',deny):
        yield

def settle(app,seconds=.13):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        app.processEvents();time.sleep(.001)

@pytest.fixture
def chat(app):
    from jarvis.ui.chat_view import ChatView
    c=ChatView();c.resize(380,300);c.show()
    for i in range(24):c.add_assistant(f'History {i}: '+'Some wrapped content. '*14)
    try:
        # Qt needs successive queued layout passes. A single busy event drain can
        # exceed a fixed sleep budget before the scroll range has been published.
        deadline=time.monotonic()+2.0
        while c.verticalScrollBar().maximum()<=1000 and time.monotonic()<deadline:
            app.processEvents();time.sleep(.001)
        assert c.verticalScrollBar().maximum()>1000
        settle(app)
        yield c
    finally:
        c.close();c.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

@pytest.mark.parametrize('position',['middle','bottom','near_bottom'])
def test_assistant_tokens_thinking_notices_follow_only_at_live_edge(chat,app,position):
    bar=chat.verticalScrollBar()
    bar.setValue(bar.maximum()//2 if position=='middle' else bar.maximum()-(8 if position=='near_bottom' else 0))
    before=bar.value()
    def check():
        settle(app)
        assert bar.value()==(before if position=='middle' else bar.maximum())
        assert not chat._scroll_timer.isActive() and not chat._scroll_settle_timer.isActive()
    bubble=chat.add_assistant_pending('Thinking');check()
    for i in range(4):
        chat.update_bubble(bubble,('Answer tokens. '*20+'\n')*(3+i));check()
    chat.update_thinking(bubble,'Reasoning. '*120);check()
    chat.collapse_thinking(bubble);check()
    chat.add_tool_details(['Tool output '*100]);check()
    chat.add_assistant('Background notice '*40);check()

@pytest.mark.parametrize('origin',['typed','voice','wake','telegram'])
def test_each_user_submission_forces_bottom_then_respects_reader_navigation(chat,app,origin):
    from jarvis.ui.main_window import MainWindow
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()//3)
    if origin=='typed':
        # Real return/send input adapter to real append helper, with command execution isolated.
        input_box=QLineEdit();input_box.setText('typed message')
        fake=SimpleNamespace(chat=chat,input=input_box,_input_history=[],_busy=False,_restart_idle_timer=lambda:None)
        fake._send_text=lambda text:MainWindow._append_user(fake,text)
        MainWindow._send_from_input(fake)
        assert input_box.text()==''
    elif origin in {'voice','wake'}:
        existing=chat.add_user('partial') if origin=='voice' else None
        fake=SimpleNamespace(chat=chat,_heard_timer=QTimer(),_live_user_bubble=existing,_listening_bubble=chat.add_assistant('Listening'))
        MainWindow._finalize_live_user_bubble(fake,'recognized command')
        assert fake._live_user_bubble is None
    else:
        from jarvis.integrations.telegram_remote import TelegramRemoteController,TELEGRAM_SUBMISSION_MARKER
        calls=[]
        fake=SimpleNamespace(chat=chat,_append_assistant=lambda *a,**k:calls.append(a))
        remote=SimpleNamespace(_agent_jobs=queue.Queue(),_notify_desktop=lambda event:MainWindow._on_telegram_event(fake,event))
        assert TelegramRemoteController._queue_agent_text(remote,'local-fake-chat','fake-user','message',label='QA')
        assert not calls
    settle(app)
    assert bar.value()==bar.maximum()
    bar.setValue(bar.maximum()//2);before=bar.value()
    bubble=chat.add_assistant('Response');chat.update_bubble(bubble,'Stream '*300);settle(app)
    assert bar.value()==before


@pytest.mark.parametrize('position',['middle','bottom'])
def test_telegram_result_and_passive_notices_respect_position(chat,app,position):
    from jarvis.ui.main_window import MainWindow
    from jarvis.integrations.telegram_remote import TELEGRAM_DIALOGUE_MARKER
    fake=SimpleNamespace(chat=chat,_append_assistant=lambda text,**kw:chat.add_assistant(text))
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()//2 if position=='middle' else bar.maximum());before=bar.value()
    for text in (TELEGRAM_DIALOGUE_MARKER+'Telegram command / completed answer','New message notification'):
        MainWindow._on_telegram_event(fake,text);settle(app)
        assert bar.value()==(before if position=='middle' else bar.maximum())


def test_rejected_telegram_queue_does_not_emit_submission():
    from jarvis.integrations.telegram_remote import TelegramRemoteController
    emitted=[];q=queue.Queue(1);q.put('full')
    fake=SimpleNamespace(_agent_jobs=q,_notify_desktop=emitted.append,_send_message=lambda *a:None,_tg_message=lambda x:x)
    assert not TelegramRemoteController._queue_agent_text(fake,'fake','fake','test',label='QA')
    assert emitted==[]


@pytest.mark.parametrize("position",["middle","top"])
def test_user_navigation_cancels_pending_submission_scroll(chat,app,position):
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()//2 if position=="middle" else 0)
    chat.scroll_to_bottom(force=True);chat.scroll_by_hotkey(-1);before=bar.value();settle(app)
    assert bar.value()==before


def test_main_window_has_no_assistant_scroll_requests():
    import ast,inspect
    from jarvis.ui.main_window import MainWindow
    tree=ast.parse(inspect.getsource(MainWindow))
    callers=[]
    for method in tree.body[0].body:
        if isinstance(method,ast.FunctionDef):
            for n in ast.walk(method):
                if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='scroll_to_bottom':callers.append(method.name)
    assert sorted(callers)==['_append_user','_dispatch_quick_media_command','_finalize_live_user_bubble','_on_telegram_event']


@pytest.mark.parametrize('enabled,heard,should_scroll',[(True,'next song',True),(False,'next song',False),(True,'панель:next',False)])
def test_quick_voice_submission_is_scrolled_only_after_acceptance(chat,app,monkeypatch,enabled,heard,should_scroll):
    import jarvis.ui.main_window as mw
    from jarvis.quick_media_commands import QuickMediaCommand
    signal=SimpleNamespace(connect=lambda callback:None)
    monkeypatch.setattr(mw,'FunctionWorker',lambda *a:SimpleNamespace(signals=SimpleNamespace(result=signal,error=signal)))
    fake=SimpleNamespace(chat=chat,_language_code=lambda:'en',_quick_media_action_enabled=lambda cmd:enabled,
        _last_quick_media_key='',_last_quick_media_at=0.,_pause_wake_listening=lambda:None,
        _suppress_media_notice=lambda *a:None,_set_status=lambda *a:None,robot=SimpleNamespace(set_state=lambda *a:None),
        _clear_listening_bubble=lambda:None,_append_assistant=lambda text,**k:chat.add_assistant(text),
        _execute_quick_media_command=lambda *a:None,_quick_media_pool=SimpleNamespace(start=lambda *a:None))
    command=QuickMediaCommand(action='next',target='auto',heard=heard)
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()//2);before=bar.value()
    mw.MainWindow._dispatch_quick_media_command(fake,command);settle(app)
    assert bar.value()==(bar.maximum() if should_scroll else before)
    if should_scroll:
        bar.setValue(bar.maximum()//3);before=bar.value()
        mw.MainWindow._dispatch_quick_media_command(fake,command);settle(app)
        assert bar.value()==before  # overlapping wake probe was deduplicated

@pytest.mark.parametrize('transcript,accepted', [('NEO open calculator',True),('open calculator',True),('NEO',False),('hmm',False),('',False)])
def test_actual_voice_recognition_adapter_only_scrolls_final_command(chat,app,transcript,accepted):
    from jarvis.ui.main_window import MainWindow
    from jarvis.wake_matcher import match_wake_phrase
    calls=[]
    fake=SimpleNamespace(chat=chat,input=QLineEdit(),_language_code=lambda:'en',
        config=SimpleNamespace(get=lambda key,default=None:'NEO' if key=='wake_word.phrase' else default),
        _voice_unlock_phrase_matches=lambda text:False,_normalize_recognized_command=lambda text,**kw:text,
        _match_wake_phrase=lambda text,phrase:match_wake_phrase(text,phrase)[:2],
        _start_command_after_acoustic_wake=lambda:None,_looks_like_linguistic_text=MainWindow._looks_like_linguistic_text,
        _continuous_voice_session=False,_live_user_bubble=None,_listening_bubble=None,_heard_timer=QTimer(),
        _clear_listening_bubble=lambda:None,_restore_listening_audio=lambda:None,_resume_wake_listening=lambda **kw:None,
        _send_recognized_text=lambda text,**kw:calls.append(text))
    fake._finalize_live_user_bubble=lambda text:MainWindow._finalize_live_user_bubble(fake,text)
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()//2);before=bar.value()
    MainWindow._voice_recognized(fake,transcript);settle(app)
    assert bar.value()==(bar.maximum() if accepted else before)
    assert bool(calls)==accepted

@pytest.mark.parametrize('scheduled',[True,False])
@pytest.mark.parametrize('local_reply',[None,'Local command completed'])
def test_timer_command_dispatch_keeps_history_while_real_user_submission_scrolls(chat,app,monkeypatch,scheduled,local_reply):
    """Exercise real timer drain -> send -> append adapters, with only execution inert."""
    import jarvis.ui.main_window as mw
    dispatched=[]
    signal=SimpleNamespace(connect=lambda callback:None)
    def worker(*args,**kwargs):
        dispatched.append(args)
        return SimpleNamespace(signals=SimpleNamespace(progress=signal,result=signal,error=signal))
    monkeypatch.setattr(mw,'FunctionWorker',worker)
    fake=SimpleNamespace(chat=chat,_language_code=lambda:'en',_busy=False,_quitting=False,
        agent=SimpleNamespace(personalize_external_response=lambda text:text),
        config=SimpleNamespace(get=lambda key,default=None:False if key=='voice.interrupt_on_wake_while_busy' else default),
        _response_active=lambda:False,_strict_stop_phrase=lambda text:False,
        _try_local_voice_control=lambda text:local_reply,_pause_wake_listening=lambda:None,
        _cancel_command_asr157=lambda:None,_arm_media_notice_suppression_for_text=lambda text:None,
        _agent_generation=0,_restart_idle_timer=lambda:None,_listening_bubble=None,
        _append_assistant=chat.add_assistant,_set_status=lambda *a:None,_rearm_voice_after_answer=lambda:None,
        _intent_animation=mw.MainWindow._intent_animation,_stream_flush_timer=QTimer(),
        robot=SimpleNamespace(set_reply_activity=lambda enabled:None),_run_agent_stream=lambda *a:None,
        _stream_response_event=lambda *a:None,_handle_agent_result=lambda *a:None,_handle_agent_error=lambda *a:None,
        thread_pool=SimpleNamespace(start=lambda worker:None),
        _timer_manager=SimpleNamespace(drain=lambda limit:[SimpleNamespace(kind='command',text='show the clock')]))
    fake._append_user=lambda text,**kwargs:mw.MainWindow._append_user(fake,text,**kwargs)
    fake._send_text=lambda text,**kwargs:mw.MainWindow._send_text(fake,text,**kwargs)
    fake._set_busy=lambda busy,*args:setattr(fake,'_busy',busy)
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()//2);before=bar.value()
    if scheduled:
        mw.MainWindow._drain_timer_events(fake)
        assert not fake._scheduled_commands172
    else:
        fake._send_text('show the clock')
    settle(app)
    assert any(item.kind=='user' and item.text=='show the clock' for item in chat.items)
    assert bar.value()==(before if scheduled else bar.maximum())
    if local_reply is None:
        assert len(dispatched)==1
        assert dispatched[0][1]=='show the clock' and dispatched[0][3]=='text'
    else:
        assert not dispatched
        assert chat.items[-1].text==local_reply


@pytest.mark.parametrize('navigation',['wheel','keyboard','slider','hotkey','value'])
def test_navigation_during_stream_cancels_queued_follow(chat,app,navigation):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum())
    bubble=chat.add_assistant('Queued response')
    if navigation=='wheel':
        event=QWheelEvent(QPointF(80,80),QPointF(80,80),QPoint(),QPoint(0,120),
                         Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
        QApplication.sendEvent(chat.viewport(),event)
    elif navigation=='keyboard':
        QTest.keyClick(chat,Qt.Key_PageUp)
    elif navigation=='slider':
        bar.setSliderDown(True);bar.setSliderPosition(bar.maximum()//2);bar.setSliderDown(False)
    elif navigation=='hotkey':chat.scroll_by_hotkey(-1)
    else:bar.setValue(bar.maximum()//2)
    before=bar.value();assert before<bar.maximum()-24
    for i in range(3):
        chat.update_bubble(bubble,('streamed line\n'*(60+i*20)))
        settle(app)
        assert bar.value()==before
    bar.setValue(bar.maximum())
    chat.update_bubble(bubble,'streamed line\n'*150);settle(app)
    assert bar.value()==bar.maximum()


@pytest.mark.parametrize('position',['middle','bottom'])
def test_resize_and_late_layout_keep_reader_or_follow_bottom(chat,app,position):
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()//2 if position=='middle' else bar.maximum())
    row=next(row for row in chat._widgets if row.y()+row.height()>bar.value())
    offset=row.y()-bar.value()
    for width in (250,650,380):
        chat.resize(width,320);settle(app)
        assert (row.y()-bar.value()==offset) if position=='middle' else (bar.value()==bar.maximum())
    bubble=chat.add_assistant('late status');settle(app)
    before=bar.value()
    QTimer.singleShot(0,lambda:bubble.setMinimumHeight(bubble.height()+550))
    settle(app)
    assert bar.value()==(before if position=='middle' else bar.maximum())


def test_late_layout_after_user_navigation_does_not_resume_follow(chat,app):
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum())
    bubble=chat.add_assistant('Deferred height')
    QTimer.singleShot(0,lambda:bubble.setMinimumHeight(1700))
    bar.setValue(bar.maximum()//3);before=bar.value()
    settle(app)
    assert bar.value()==before
    assert not chat._scroll_timer.isActive() and not chat._scroll_settle_timer.isActive()


def test_long_history_eviction_preserves_visible_message_and_selection(chat,app):
    for i in range(170):chat.add_assistant(f'Old line {i}: '+'history '*12)
    settle(app,.3)
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()//2)
    row=next(row for row in chat._widgets if row.y()+row.height()>bar.value())
    from jarvis.ui.chat_view import MessageBubble
    label=row.findChild(MessageBubble).body_label
    label.setSelection(0,8);selected=label.selectedText();offset=row.y()-bar.value()
    for i in range(5):
        chat.add_assistant(f'Incoming line {i}\n'+'reply '*100);settle(app)
        assert row.y()-bar.value()==offset
        assert label.selectedText()==selected
        assert len(chat.items)==len(chat._widgets)==180
    chat.scroll_to_bottom(force=True);settle(app)
    assert bar.value()==bar.maximum()
    assert label.selectedText()==selected
    chat.add_assistant('Final background status');settle(app)
    assert bar.value()==bar.maximum()


def test_pending_follow_cannot_survive_clear_or_close(app):
    from jarvis.ui.chat_view import ChatView
    chat=ChatView();chat.resize(380,300);chat.show()
    chat.add_assistant('Pending');chat.scroll_to_bottom(force=True)
    chat.clear_messages();settle(app)
    assert not chat.items and not chat._widgets
    chat.add_assistant('Fresh\n'*100);settle(app)
    assert chat.verticalScrollBar().value()==chat.verticalScrollBar().maximum()
    chat.scroll_to_bottom(force=True);chat.close();chat.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);settle(app)


@pytest.mark.parametrize('navigation',['wheel','pixel_wheel','end','down','page_down','hotkey'])
def test_noop_downward_navigation_at_bottom_keeps_later_following(chat,app,navigation):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum())
    if navigation in {'wheel','pixel_wheel'}:
        event=QWheelEvent(QPointF(80,80),QPointF(80,80),
                         QPoint(0,-20) if navigation=='pixel_wheel' else QPoint(),
                         QPoint(0,-120) if navigation=='wheel' else QPoint(),
                         Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
        QApplication.sendEvent(chat.viewport(),event)
    elif navigation=='hotkey':chat.scroll_by_hotkey(1)
    else:QTest.keyClick(chat,{'end':Qt.Key_End,'down':Qt.Key_Down,'page_down':Qt.Key_PageDown}[navigation])
    assert bar.value()==bar.maximum()
    chat.add_assistant('New status\n'*10);settle(app)
    assert bar.value()==bar.maximum()


@pytest.mark.parametrize('distance',[0,23,24,25])
def test_preappend_live_edge_threshold(chat,app,distance):
    bar=chat.verticalScrollBar();bar.setValue(bar.maximum()-distance);before=bar.value()
    chat.add_assistant('New status\n'*12);settle(app)
    assert bar.value()==(bar.maximum() if distance<=24 else before)


@pytest.mark.parametrize('clamp',['resize_to_fit','shrink_content'])
def test_layout_clamped_reader_follows_next_overflowing_append(chat,app,clamp):
    bar=chat.verticalScrollBar()
    if clamp=='resize_to_fit':
        bar.setValue(bar.maximum()//2)
        chat.resize(380,10000);settle(app)
        assert bar.maximum()==0
    else:
        bubble=chat.add_assistant('Long line\n'*100);settle(app)
        bar.setValue(bar.maximum()-120)
        chat.update_bubble(bubble,'Collapsed');settle(app)
    assert bar.value()==bar.maximum()
    chat.add_assistant('Large incoming reply\n'*600);settle(app)
    assert bar.maximum()>0 and bar.value()==bar.maximum()


def test_navigation_between_queued_callbacks_still_wins(chat,app):
    bar=chat.verticalScrollBar();position=bar.maximum()//3
    chat.scroll_to_bottom(force=True)
    chat._scroll_timer.timeout.connect(lambda:bar.setValue(position))
    chat.add_assistant('Pending status\n'*30);settle(app)
    assert bar.value()==position
    assert not chat._scroll_timer.isActive() and not chat._scroll_settle_timer.isActive()


def test_noop_upward_navigation_still_cancels_deferred_scroll(app):
    from jarvis.ui.chat_view import ChatView
    chat=ChatView();chat.resize(380,300);chat.show();settle(app)
    try:
        bubble=chat.add_assistant('Queued reply\n'*80)
        chat.scroll_to_bottom(force=True)
        # No range has been published yet, so Up cannot move. Its cancellation
        # must survive both layout timers and another incoming content update.
        chat.scroll_by_hotkey(-1)
        chat.update_bubble(bubble,'Queued reply\n'*100);settle(app)
        assert chat.verticalScrollBar().value()==0
        chat.scroll_to_bottom(force=True);settle(app)
        assert chat.verticalScrollBar().value()==chat.verticalScrollBar().maximum()
    finally:
        chat.close();chat.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
