from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import ast,json
import pytest
from tools.verify_speech import score_transcript,summarize,read_wav
ROOT=Path(__file__).resolve().parents[1]

def test_transcript_wake_tail_independent():
 r=score_transcript({'expected':'Jarvis play music','wake':True,'expected_command':'play music'},'Jarvis stop music')
 assert r['checks']=={'transcript':False,'wake':True,'command_tail':False} and not r['passed']
 assert score_transcript({'expected':'Jarvis','wake':False},'Jarvis')['passed'] is False
 assert score_transcript({'wake':True,'expected_command':''},'')['passed'] is False

@pytest.mark.parametrize('value',['false',0,None])
def test_wake_expectation_is_boolean(value):
 with pytest.raises(ValueError):score_transcript({'wake':value},'Jarvis')

@pytest.mark.parametrize('value',[float('nan'),float('inf'),-.1,2])
def test_wer_threshold_is_validated(value):
 with pytest.raises(ValueError):score_transcript({'expected':'hello','max_wer':value},'hello')

def test_wer_is_word_weighted_and_cold_latency_separate():
 a=score_transcript({'expected':'one'},'wrong');b=score_transcript({'expected':'one two three four five six seven eight nine'},'one two three four five six seven eight nine')
 a.update(asr_ms=9000,cold_model_call=True,audio_seconds=1);b.update(asr_ms=100,cold_model_call=False,audio_seconds=3)
 r=summarize([a,b]);assert r['corpus_word_error_rate']==.1 and r['warm_p95_asr_ms']==100
 assert summarize([score_transcript({},'hello')])['corpus_word_error_rate'] is None

def test_acoustic_zero_confidence_and_unicode_are_valid():
 from jarvis.stt_worker import _decoded_text_and_quality
 r=_decoded_text_and_quality([SimpleNamespace(text='Öffne Straße œuvre',avg_logprob=0.,no_speech_prob=0.)],1,'de')
 assert r[3]['avg_logprob']==0 and r[3]['weird_ratio']==0

def ui_method(name):
 source=ast.parse((ROOT/'jarvis/ui/main_window.py').read_text())
 cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
 method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name)
 method.decorator_list=[]
 ns={'logger':__import__('logging').getLogger('test155'),'traceback':__import__('traceback')};exec(compile(ast.Module(body=[method],type_ignores=[]),'<actual UI method>','exec'),ns)
 return ns[name]

@pytest.mark.parametrize('state', ['fresh','cancelled','new_capture','muted','quitting'])
def test_late_command_results_cannot_execute(state):
 dispatch=ui_method('_deliver_command_asr155')
 obj=SimpleNamespace(_quitting=state=='quitting',_app_mic_muted=state=='muted',_command_asr_generation=2 if state=='new_capture' else 1,_agent_generation=3 if state=='cancelled' else 2,_command_transcription_ready=Mock(),_command_asr_failed157=Mock())
 dispatch(obj,{'text':'open file'},1,2)
 assert obj._command_transcription_ready.call_count==(1 if state=='fresh' else 0)
 dispatch(obj,{'text':'duplicate'},1,2)
 assert obj._command_transcription_ready.call_count==(1 if state=='fresh' else 0)

def test_nested_plugin_dispatch_uses_registry_guard():
 from jarvis.plugins.api import PluginDispatchRegistry
 owner=SimpleNamespace(config=None,database=None,ollama=None,security=None,execute=Mock(return_value='guarded'),get=Mock(return_value='tool'),risk_for=Mock(return_value='risk'))
 facade=PluginDispatchRegistry(owner,{})
 assert facade.execute('browser',{'action':'status'})=='guarded'
 owner.execute.assert_called_once_with('browser',{'action':'status'})
 assert facade.get('browser')=='tool' and facade.risk_for('browser',{})=='risk'

def test_all_new_regressions_reach_test_cmd_inventory():
 from tools.atlas_all_test import load_cases
 from collections import Counter
 counts=Counter()
 for code in ['ru','en','de','fr','es','it','pt']:
  counts[code]=sum(r.source=='natural155' for r in load_cases(ROOT,code))
 assert set(counts.values())=={45}


def test_linguistic_filter_keeps_static_binding():
    import ast
    from pathlib import Path
    source = Path(__file__).resolve().parents[1]/"jarvis/ui/main_window.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_looks_like_linguistic_text")
    assert any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in method.decorator_list)
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "filter", "exec"), {"re": __import__("re")}, namespace)
    window = type("FilterHarness", (), namespace)()
    assert window._looks_like_linguistic_text("включи музыку")
    assert not window._looks_like_linguistic_text("...")
