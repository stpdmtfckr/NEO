"""Muted voice must not verify/load a model or queue any synthesis."""
from __future__ import annotations
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from jarvis.testing.isolation import external_io_blocked
from jarvis.response_voice.service import VoiceService
from tests.test_response_voice_qwen import Config,ready_store,StrictProcess,until


@pytest.fixture(autouse=True)
def inert():
    with external_io_blocked():yield


@pytest.mark.parametrize('language',['ru','en','de','fr','es','it','pt'])
@pytest.mark.parametrize('speaker',['Aiden','Serena'])
def test_zero_volume_skips_queue_verification_and_helper(tmp_path,language,speaker):
    config=Config(tmp_path,volume=0,speaker=speaker);store=ready_store(tmp_path)
    factory=Mock(side_effect=AssertionError('muted voice must not launch a helper'))
    service=VoiceService(config,store=store,process_factory=factory,runtime_check=lambda p:True)
    store.verification_error=Mock(side_effect=AssertionError('muted submission must not probe models'))
    store.ready_cached=Mock(side_effect=AssertionError('muted submission must not verify models'))
    before=service.snapshot()
    assert not service.submit('Muted','en')
    assert not service.submit('Muted preview',language,preview=True)
    assert service._thread is None and service._queue.empty() and service.snapshot()==before
    factory.assert_not_called();store.verification_error.assert_not_called();store.ready_cached.assert_not_called()
    service.close()


def test_preview_zero_override_is_silent_without_changing_saved_volume(tmp_path):
    config=Config(tmp_path,volume=80);factory=Mock()
    service=VoiceService(config,store=ready_store(tmp_path),process_factory=factory,runtime_check=lambda p:True)
    assert not service.submit('Preview','en',preview=True,overrides={'volume':0})
    assert config.get('response_voice.volume')==80 and service._thread is None
    assert not service.active;factory.assert_not_called();service.close()


def test_volume_reenable_waits_for_next_request_then_uses_new_volume(tmp_path):
    config=Config(tmp_path,volume=0);made=[]
    def factory(*args):process=StrictProcess();made.append(process);return process
    service=VoiceService(config,store=ready_store(tmp_path),process_factory=factory,runtime_check=lambda p:True)
    try:
        assert not service.submit('Muted','en')
        config.data['response_voice']['volume']=35;service.apply_preferences()
        assert not made and not service.active
        assert service.submit('Audible','en');until(lambda:made and made[0].sent)
        assert made[0].sent[0]['volume']==35
        made[0].events.put({'state':'done'});until(lambda:service._thread is None)
    finally:service.close()


@pytest.mark.parametrize('change',['mute','disable'])
def test_apply_cancels_active_and_queued_speech_and_reenable_recovers(tmp_path,change):
    config=Config(tmp_path,volume=80);made=[]
    def factory(*args):process=StrictProcess();made.append(process);return process
    service=VoiceService(config,store=ready_store(tmp_path),process_factory=factory,runtime_check=lambda p:True)
    try:
        assert service.submit('First','en');until(lambda:made and made[0].sent)
        made[0].events.put({'state':'playing'});until(lambda:service.snapshot()['playing'])
        assert service.submit('Discard queued reply','en')
        if change=='mute':config.data['response_voice']['volume']=0
        else:config.data['response_voice']['enabled']=False
        service.apply_preferences();service.apply_preferences()
        assert made[0].closed and service._process is None and service._queue.empty()
        assert service.snapshot()['voice_state']=='idle' and not service.active
        until(lambda:made[0].reaped)
        assert made[0].reap_count==1
        config.data['response_voice'].update(enabled=True,volume=25);service.apply_preferences()
        assert service.submit('Second','en');until(lambda:len(made)==2 and made[1].sent)
        assert made[1].sent[0]['text']=='Second' and made[1].sent[0]['volume']==25
    finally:
        service.close();until(lambda:all(p.reaped for p in made))


def test_main_window_apply_uses_voice_preference_hook():
    path=Path(__file__).resolve().parents[1]/'jarvis/ui/main_window.py'
    klass=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
    method=next(n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name=='_apply_settings')
    method.body=method.body[:2]  # The narrow voice hook precedes the unrelated window/theme work.
    namespace={};exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),str(path),'exec'),namespace)
    service=SimpleNamespace(apply_preferences=Mock())
    namespace['_apply_settings'](SimpleNamespace(_response_voice_service=service))
    service.apply_preferences.assert_called_once_with()
    namespace['_apply_settings'](SimpleNamespace())
