"""Independent field regressions: real Qt scheduling and complete user turns.

Microphone/model/account transports are not available in this test. Decode jobs
use explicit controlled blockers; production callbacks and Qt delivery are real.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def qt_app():
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def pump(app, predicate, seconds=3):
    deadline=time.monotonic()+seconds
    while not predicate() and time.monotonic()<deadline:
        app.processEvents()
        time.sleep(.002)
    app.processEvents()
    assert predicate(), 'Qt result/timeout did not arrive before the test deadline'


def test_command_decode_and_delivery_survive_exhausted_global_worker_pool():
    from PySide6.QtCore import QThreadPool, QRunnable
    from jarvis.ui.command_asr157 import CommandAsrRunner
    app=qt_app(); pool=QThreadPool.globalInstance(); original=pool.maxThreadCount()
    release=threading.Event(); started=threading.Event(); completed=[]; failures=[]
    class Blocker(QRunnable):
        def run(self):
            started.set(); release.wait(3)
    pool.setMaxThreadCount(1); pool.start(Blocker())
    assert started.wait(1)
    runner=CommandAsrRunner(); main_thread=threading.get_ident()
    try:
        assert runner.start(lambda cancelled: {'text':'привет','decode_thread':threading.get_ident()},
            result=lambda value: completed.append((value,threading.get_ident())),
            error=failures.append,timeout=1,abort=release.set)
        pump(app,lambda:bool(completed or failures))
        assert not failures and completed[0][0]['text']=='привет'
        assert completed[0][0]['decode_thread']!=main_thread
        assert completed[0][1]==main_thread
        assert not release.is_set(), 'The command waited for a network/pool slot'
    finally:
        release.set(); runner.cancel(); pool.waitForDone(3000)
        pool.setMaxThreadCount(original); runner.deleteLater();app.processEvents()


def test_timeout_discards_old_text_then_accepts_the_next_command():
    from jarvis.ui.command_asr157 import CommandAsrRunner
    app=qt_app(); runner=CommandAsrRunner(); release=threading.Event()
    finished=threading.Event(); replies=[]; errors=[]; aborts=[]
    def slow(cancelled):
        release.wait(3);finished.set();return 'obsolete transcript'
    try:
        assert runner.start(slow,result=replies.append,error=errors.append,timeout=.06,
                            abort=lambda:aborts.append(threading.get_ident()))
        pump(app,lambda:bool(errors))
        assert len(errors)==1 and len(aborts)==1 and aborts[0]==threading.get_ident()
        assert not replies
        # A native call that has not returned cannot spawn an unbounded second
        # decoder. Once it exits, a fresh utterance is accepted normally.
        assert not runner.start(lambda _: 'parallel',result=replies.append,error=errors.append,
                                timeout=.1,abort=lambda:None)
        release.set();assert finished.wait(1)
        pump(app,lambda:not runner._thread.is_alive())
        assert runner.start(lambda _: 'fresh transcript',result=replies.append,error=errors.append,
                            timeout=1,abort=lambda:None)
        pump(app,lambda:bool(replies))
        assert replies==['fresh transcript'] and len(errors)==1
    finally:
        release.set();runner.cancel();runner.deleteLater();app.processEvents()


def test_cancelled_decode_never_replaces_a_later_text_turn():
    from jarvis.ui.command_asr157 import CommandAsrRunner
    app=qt_app();runner=CommandAsrRunner();release=threading.Event();finished=threading.Event()
    replies=[];errors=[];aborts=[]
    def decode(cancelled):
        release.wait(3);finished.set();return 'old voice command'
    try:
        assert runner.start(decode,result=replies.append,error=errors.append,timeout=1,
                            abort=lambda:aborts.append(True))
        runner.cancel();release.set();assert finished.wait(1)
        pump(app,lambda:not runner._thread.is_alive())
        assert not replies and not errors and aborts==[True]
        assert runner.start(lambda _: 'new voice command',result=replies.append,error=errors.append,
                            timeout=1,abort=lambda:None)
        pump(app,lambda:bool(replies))
        assert replies==['new voice command']
    finally:
        release.set();runner.cancel();runner.deleteLater();app.processEvents()


@pytest.fixture
def reviewed_agent(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session=AtlasSession(tmp_path/'review_run')
        yield session
        session.finish()


def full_voice_window(session):
    """Real voice dispatch + Agent + response completion; inert widget/IO edges."""
    from types import MethodType
    from unittest.mock import Mock
    from PySide6.QtCore import QThreadPool, QTimer
    from tests.test_neo157_voice import VoiceHarness
    from jarvis.ui.main_window import MainWindow
    class Chat:
        def __init__(self):self.rows=[]
        def add_assistant_pending(self,text):
            row={'role':'assistant','text':text};self.rows.append(row);return row
        def add_user(self,text):
            row={'role':'user','text':text};self.rows.append(row);return row
        def update_bubble(self,row,text):row['text']=text
        def collapse_thinking(self,*a):pass
        def scroll_to_bottom(self, *, force=False):pass
        def move_bubble_to_bottom(self,row):self.rows.remove(row);self.rows.append(row)
    w=VoiceHarness();w.agent=session.agent;w.config=session.config
    w.config.data.setdefault('ui',{})['character_streaming_enabled']=False
    w.config.data.setdefault('voice',{})['fast_media_enabled']=False
    w.config.data['voice']['interrupt_on_wake_while_busy']=False
    w.config.data['wake_word']['phrase']='Джарвис'
    for name in ('_send_text','_run_agent_stream','_handle_agent_result','_handle_agent_error',
        '_stream_response_event','_flush_response_stream','_handle_agent_turn',
        '_finish_response_ui','_rearm_voice_after_answer','_finalize_live_user_bubble'):
        setattr(w,name,MethodType(getattr(MainWindow,name),w))
    w.chat=Chat();w._listening_bubble=None;w._pending_response_bubble=None
    w._stream_pending_text='';w._stream_text='';w._stream_received=False
    w._stream_flush_timer=QTimer(w);w._ambient_typing_timer=QTimer(w)
    w._ambient_typing_bubble=None;w._ambient_typing_queue=[]
    w.send_button=Mock();w.settings_button=Mock();w.reset_button=None;w.robot=Mock()
    w._response_active=lambda:w._busy
    w._strict_stop_phrase=lambda text:False;w._try_local_voice_control=lambda text:None
    w._pause_wake_listening=Mock();w._arm_media_notice_suppression_for_text=Mock()
    w._intent_animation=MainWindow._intent_animation;w._localize_text=lambda text:text
    w._append_tool_details=Mock();w._finish_speech_stream=Mock();w._feed_speech_stream=Mock()
    w._mirror_assistant_to_telegram=Mock();w._infer_response_emotion=lambda *a:'idle'
    w._set_status=Mock();w._restart_idle_timer=Mock();w._wake_guard=Mock()
    w._resume_wake_listening=Mock();w._start_interrupt_wake_monitor=Mock()
    w._handle_error=Mock(side_effect=lambda trace:pytest.fail(trace))
    w.thread_pool=QThreadPool(w)
    return w


def test_greeting_from_final_audio_reaches_real_agent_reply_and_idle(reviewed_agent):
    import numpy as np
    from unittest.mock import Mock
    from jarvis.voice import RecordingResult
    app=qt_app();w=full_voice_window(reviewed_agent)
    original=reviewed_agent.agent.handle_stream
    reviewed_agent.agent.handle_stream=Mock(wraps=original)
    try:
        w._transcribe_command(RecordingResult(np.ones(48000,dtype=np.float32),16000))
        pump(app,lambda:bool(w._resume_wake_listening.called),seconds=10)
        assert not w._busy and not w._command_asr_pending157
        assert w._pending_response_bubble is None
        assert [r['text'] for r in w.chat.rows if r['role']=='user']==['привет']
        replies=[r['text'] for r in w.chat.rows if r['role']=='assistant']
        assert len(replies)==1 and replies[0].strip()
        assert replies[0] not in {'Думаю','Думает…','Распознаю вашу речь…'}
        assert 'SearchAPI' not in replies[0]
        assert reviewed_agent.agent.handle_stream.call_args.args[0]=='привет'
        assert not reviewed_agent.calls
        w.robot.set_state.assert_called_with('idle')
    finally:
        w._cancel_command_asr157();w.thread_pool.waitForDone(3000);w.deleteLater();app.processEvents()


def test_voice_timeout_then_retry_reaches_one_real_greeting_response(reviewed_agent):
    import numpy as np
    from unittest.mock import Mock
    from jarvis.voice import RecordingResult, TranscriptionResult
    app=qt_app();w=full_voice_window(reviewed_agent);release=threading.Event()
    original=reviewed_agent.agent.handle_stream
    reviewed_agent.agent.handle_stream=Mock(wraps=original)
    w.recognizer.transcribe_detailed.side_effect=lambda rec:(release.wait(3),TranscriptionResult('obsolete',[]))[1]
    rec=RecordingResult(np.ones(48000,dtype=np.float32),16000)
    try:
        w._transcribe_command(rec)
        w._command_asr_runner157._job.deadline=time.monotonic()+.06
        pump(app,lambda:w._resume_wake_listening.called)
        assert not w._busy and not w._command_asr_pending157
        assert not reviewed_agent.agent.handle_stream.called
        assert 'повторите' in w._append_assistant.call_args.args[0].casefold()
        release.set();pump(app,lambda:not w._command_asr_runner157._thread.is_alive())
        w._resume_wake_listening.reset_mock()
        w.recognizer.transcribe_detailed.side_effect=None
        w.recognizer.transcribe_detailed.return_value=TranscriptionResult('Джарвис, привет',[])
        w._transcribe_command(rec)
        pump(app,lambda:w._resume_wake_listening.called,seconds=10)
        assert reviewed_agent.agent.handle_stream.call_count==1
        assert not w._busy and w._pending_response_bubble is None
        assert [r['text'] for r in w.chat.rows if r['role']=='user']==['привет']
        replies=[r['text'] for r in w.chat.rows if r['role']=='assistant']
        assert len(replies)==1 and replies[0].strip() and 'obsolete' not in str(w.chat.rows)
    finally:
        release.set();w._cancel_command_asr157();w.thread_pool.waitForDone(3000);w.deleteLater();app.processEvents()


@pytest.mark.parametrize('head,answers,tool,action,wanted',[
    ('громкость',['35'],'audio','set_volume',{'value':35}),
    ('найди файл',['report.txt'],'search','files',{'query':'report.txt'}),
    ('будильник',['08:30'],'timer','alarm',{'text':'08:30'}),
    ('напомни',['5 минут','проверить отчёт'],'timer','reminder',{'seconds':300,'label':'проверить отчёт'}),
])
def test_short_task_clarifies_then_executes_exact_intended_arguments(reviewed_agent,head,answers,tool,action,wanted):
    session=reviewed_agent;session.reset_case('ru')
    for text in [head]+answers[:-1]:
        turn=session.agent.handle(text)
        pending=session.agent.conversations.get('desktop').pending_intent
        assert pending and (pending.tool,pending.arguments.get('action'))==(tool,action),turn.text
        assert not session.calls and not turn.pending
    turn=session.agent.handle(answers[-1])
    assert len(session.calls)==1,(turn.text,session.calls)
    call=session.calls[0]
    assert (call['tool'],call['arguments']['action'])==(tool,action)
    assert all(call['arguments'].get(key)==value for key,value in wanted.items()),call
    assert session.agent.conversations.get('desktop').pending_intent is None


from tests.test_media_reliability import local_adapter


def test_manual_next_preserves_tracks_refilled_during_playback_preparation(local_adapter,monkeypatch):
    from tests.test_media_reliability import Track
    adapter,player=local_adapter
    adapter._current_track_obj=Track('A');adapter._current_track_meta={'id':'A','title':'A'}
    adapter._station='user:onyourwave';adapter._auto_advance=True
    adapter._station_queue=[Track('B'),Track('C')]
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    calls=[]
    def play(*args,**kwargs):
        calls.append(args)
        # Background Rotor has completed while the real foreground transition
        # is waiting for player verification. This row was not in its snapshot.
        with adapter._playback_lock:
            adapter._station_queue.append(Track('D'))
        return {'verified':True,'state':'playing','duration_ms':180000}
    player.play=play
    result=adapter._next_background(auto=False)
    assert result['title']=='B' and len(calls)==1
    assert [track.id for track in adapter._station_queue]==['C','D']


def notice_window():
    from types import MethodType
    from unittest.mock import Mock
    from jarvis.ui.main_window import MainWindow
    w=SimpleNamespace(config=SimpleNamespace(get=lambda key,default=None:default),
        _language_code=lambda:'ru',_last_now_playing_by_source={},_media_notice_suppressed_until={},
        _now_playing_queue_preview=lambda source:'Дальше в очереди: Real next song',
        _append_assistant=Mock(),_refresh_robot_stage_info=Mock(),_pending_media_results157={})
    w._drain_media_now_playing_events=MethodType(MainWindow._drain_media_now_playing_events,w)
    w._recover_media_notice157=MethodType(MainWindow._recover_media_notice157,w)
    w._remember_media_bubble157=MethodType(MainWindow._remember_media_bubble157,w)
    w._refresh_media_bubble_queue157=MethodType(MainWindow._refresh_media_bubble_queue157,w)
    w.chat=Mock()
    return w


def test_stale_committed_track_is_announced_after_suppressed_event_only_once():
    from jarvis.runtime.media_events import capture_media_events, command_result_owns_media_notice, drain_now_playing, publish_now_playing
    drain_now_playing(128);w=notice_window()
    with capture_media_events() as events, command_result_owns_media_notice():
        publish_now_playing('yandex_music','Committed song','Artist','review-stale')
    w._pending_media_results157[1]=tuple(events)
    w._drain_media_now_playing_events()
    w._append_assistant.assert_not_called()
    w._recover_media_notice157(1)
    w._append_assistant.assert_called_once()
    assert 'Committed song' in w._append_assistant.call_args.args[0]
    assert 'Real next song' in w._append_assistant.call_args.args[0]
    w._recover_media_notice157(1)
    w._append_assistant.assert_called_once()


def test_stale_completion_does_not_duplicate_already_announced_track():
    from jarvis.runtime.media_events import capture_media_events, drain_now_playing, publish_now_playing
    drain_now_playing(128);w=notice_window()
    with capture_media_events() as events:
        publish_now_playing('yandex_music','Already visible song','Artist','review-announced')
    w._pending_media_results157[1]=tuple(events)
    w._drain_media_now_playing_events()
    w._append_assistant.assert_called_once()
    w._recover_media_notice157(1)
    w._append_assistant.assert_called_once()


@pytest.mark.parametrize('replacement',[('New song','review-new',True),('Old song','review-old',False),('Old song','review-old',True)])
def test_stale_media_completion_never_reannounces_replaced_paused_or_restarted_event(replacement):
    from jarvis.runtime.media_events import capture_media_events, command_result_owns_media_notice, drain_now_playing, publish_now_playing
    drain_now_playing(128);w=notice_window()
    with capture_media_events() as events, command_result_owns_media_notice():
        publish_now_playing('yandex_music','Old song','Artist','review-old')
    w._pending_media_results157[1]=tuple(events)
    title,track_id,playing=replacement
    publish_now_playing('yandex_music',title,'Artist',track_id,playing=playing)
    drain_now_playing(128)
    w._recover_media_notice157(1)
    w._append_assistant.assert_not_called()


def test_verified_file_reference_refuses_replaced_content(reviewed_agent):
    session=reviewed_agent;session.reset_case('ru')
    path=session.workspace/'review-original.txt';path.write_text('original contents',encoding='utf-8')
    args={'action':'read_text','source':str(path)}
    session._permitted.append(('filesystem',args))
    first=session.agent.handle('прочитай файл "'+str(path)+'"')
    assert first.success,first.text
    state=session.agent.conversations.get('desktop')
    assert any(row['value']==str(path) for row in state.objects157)
    # Same filename is no longer the object for which authority was established.
    path.write_text('externally replaced contents with a different size',encoding='utf-8')
    session.calls.clear()
    turn=session.agent.handle('удали его')
    assert not session.calls and not turn.pending
    assert 'изменился' in turn.text.casefold() and path.exists()


def test_multiple_verified_files_require_choice_and_keep_exact_selected_identity(reviewed_agent):
    session=reviewed_agent;session.reset_case('ru')
    paths=[session.workspace/name for name in ('alpha.txt','beta.txt')]
    for index,path in enumerate(paths):
        path.write_text('read fixture '+str(index),encoding='utf-8')
        session._permitted.append(('filesystem',{'action':'read_text','source':str(path)}))
        turn=session.agent.handle('прочитай файл "'+str(path)+'"')
        assert turn.success,turn.text
    session.calls.clear()
    turn=session.agent.handle('прочитай его')
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending and pending.tool=='object_request157',turn.text
    assert not session.calls and 'alpha.txt' in turn.text and 'beta.txt' in turn.text
    # Most recently verified file is first, but no file executes before selection.
    turn=session.agent.handle('1')
    assert len(session.calls)==1 and session.calls[0]['tool']=='filesystem'
    assert session.calls[0]['arguments']=={'action':'read_text','source':str(paths[-1])}


def test_real_agent_music_story_queue_next_previous_stop_resume(reviewed_agent,local_adapter,monkeypatch):
    """Only Rotor and physical player IO are fixtures; routing and adapter run."""
    from unittest.mock import Mock
    from tests.test_media_reliability import Track
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    from jarvis.runtime import media_activity,media_events
    monkeypatch.setattr(media_activity,'_STATE',{'owner':'','state':'','updated':0.0})
    monkeypatch.setattr(media_activity,'_OWNERS',{})
    monkeypatch.setattr(media_events,'_LATEST_BY_SOURCE',{})
    session=reviewed_agent;session.reset_case('ru');adapter,player=local_adapter
    session.config.data['integrations']['yandex_music']['enabled']=True
    adapter.config.values['integrations.yandex_music.enabled']=True
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    monkeypatch.setattr(adapter,'wave_tracks',lambda **kw:{'tracks':[Track(x) for x in 'ABCDEF'],'batch_id':'review-batch'})
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    state={'state':'stopped','position_ms':0,'duration_ms':180000}
    def play(source,**kwargs):
        state.update(state='playing',position_ms=1000,title=kwargs.get('title',''))
        return dict(state,verified=True)
    def transport(target):
        state['state']=target
        return dict(state,verified=True)
    player.play=Mock(side_effect=play)
    player.status_if_running=lambda:dict(state)
    player.pause=Mock(side_effect=lambda:transport('paused'))
    player.stop=Mock(side_effect=lambda:transport('stopped'))
    player.resume=Mock(side_effect=lambda:transport('playing'))
    entries={}
    register_browser_media_tools(SimpleNamespace(register=lambda definition:entries.setdefault(definition.name,definition)),
        config=session.config,yandex_adapter=adapter,spotify_adapter=Mock(),youtube_adapter=Mock(),discord_adapter=Mock())
    for name in ('yandex_music','music'):
        session.agent.tools.get(name).handler=entries[name].handler
    def ask(phrase,tool,action):
        session.calls.clear();session._permitted.append((tool,{'action':action}))
        result=session.agent.handle(phrase)
        assert result.success and not result.pending,(phrase,result.text,session.calls)
        assert len(session.calls)==1 and session.calls[0]['tool']==tool
        assert session.calls[0]['arguments']=={'action':action}
        assert 'SearchAPI' not in result.text
        return result.text
    try:
        text=ask('моя волна','yandex_music','my_wave')
        assert 'Artist — A' in text.splitlines()[0] and '1. Artist — B' in text and player.play.call_count==1
        for phrase in ('очередь музыка','какие треки дальше','очередь музыки компьютер'):
            text=ask(phrase,'music','queue_status')
            assert 'Artist — A' in text.splitlines()[0] and '1. Artist — B' in text and player.play.call_count==1
        text=ask('дальше','music','next')
        assert 'Artist — B' in text.splitlines()[0] and '1. Artist — C' in text and player.play.call_count==2
        text=ask('назад','music','previous')
        assert 'Artist — A' in text.splitlines()[0] and '1. Artist — B' in text and player.play.call_count==3
        ask('стоп','music','stop')
        assert player.stop.call_count==1 and adapter._manual_pause_active
        ask('продолжить','music','play')
        assert player.resume.call_count==1 and not adapter._manual_pause_active
        assert player.play.call_count==3 and adapter._current_track_obj.id=='A'
    finally:
        media_events.drain_now_playing(128)


def test_note_rename_without_context_cannot_turn_new_title_into_content(reviewed_agent):
    session=reviewed_agent;session.reset_case('ru')
    turn=session.agent.handle('переименуй эту заметку')
    assert not session.calls and not turn.pending
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending is None or all(slot.name!='content' for slot in pending.slots)


def test_note_version_change_after_rename_question_blocks_update(reviewed_agent):
    session=reviewed_agent;session.reset_case('ru')
    for phrase in ('создай заметку','Review note'):
        turn=session.agent.handle(phrase)
        assert session.agent.conversations.get('desktop').pending_intent and not session.calls
    session._permitted.append(('shelf',{'action':'create','kind':'note','title':'Review note','text':'original body'}))
    turn=session.agent.handle('original body')
    assert turn.success,(turn.text,session.calls)
    state=session.agent.conversations.get('desktop')
    notes=[row for row in state.objects157 if row['kind']=='note']
    assert len(notes)==1 and notes[0].get('version') is not None
    note=notes[0];session.calls.clear()
    turn=session.agent.handle('переименуй эту заметку')
    pending=state.pending_intent
    assert pending and pending.missing().name=='title' and pending.arguments['id']==note['value']
    assert not session.calls
    edit=session.agent.tools.get('shelf').handler({'action':'update','id':note['value'],
        'version':note['version'],'content':'externally modified body'})
    assert edit.success,edit.text
    turn=session.agent.handle('New review title')
    assert not session.calls and not turn.pending
    assert 'изменился' in turn.text.casefold() or 'изменилась' in turn.text.casefold(),turn.text


@pytest.mark.parametrize('phrase',['дальше','назад','продолжить'])
def test_bare_social_followup_cannot_control_inactive_configured_spotify(reviewed_agent,monkeypatch,phrase):
    from jarvis.runtime import media_activity,media_events
    monkeypatch.setattr(media_activity,'_STATE',{'owner':'','state':'','updated':0.0})
    monkeypatch.setattr(media_activity,'_OWNERS',{})
    monkeypatch.setattr(media_events,'_LATEST_BY_SOURCE',{})
    session=reviewed_agent;session.reset_case('ru')
    session.config.data['integrations']['yandex_music']['enabled']=False
    session.config.data['integrations']['spotify']['enabled']=True
    greeting=session.agent.handle('расскажи шутку')
    assert greeting.text and not session.calls
    answer=session.agent.handle(phrase)
    assert not session.calls,(answer.text,session.calls)
    assert answer.text.strip() and not answer.pending
