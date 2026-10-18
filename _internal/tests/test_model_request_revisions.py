"""Review regressions: observed Settings revisions, fresh inventory, custom choice."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import threading
import pytest
import requests

import jarvis.ollama_client as oc
from jarvis.runtime.cancellation import OperationCancelled, cancellation_manager
if __package__:
    from .test_model_request_fixes import Config, Session, Response, MODES, invoke, client
else:
    from test_model_request_fixes import Config, Session, Response, MODES, invoke, client


def assign(data,key,value):
    parts=key.split('.')
    current=data
    for part in parts[:-1]:current=current.setdefault(part,{})
    current[parts[-1]]=value

@pytest.fixture
def saved_client(tmp_path,monkeypatch):
    from jarvis.config import ConfigManager
    from jarvis.agent import Agent
    from jarvis.runtime.router import hybrid_router
    for key in ('HOME','LOCALAPPDATA','APPDATA','XDG_CACHE_HOME'):
        monkeypatch.setenv(key,str(tmp_path/key))
    monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *_:None)
    monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *_:None)
    root=Path(oc.__file__).resolve().parents[1]
    cfg=ConfigManager(root/'config/default_settings.json')
    data=deepcopy(cfg.data)
    for key,value in Config().values.items():assign(data,key,value)
    cfg.save(data)
    c=oc.OllamaClient(cfg);s=Session();c._session=s
    monkeypatch.setattr(oc.requests,'Session',lambda:s)
    # Real ConfigManager Save/reload and the real Agent refresh integration.
    # Construction-only domain/UI/hardware dependencies are irrelevant here.
    agent=SimpleNamespace(config=cfg,ollama=c,context_memory=None,router=hybrid_router(),
                          game_web=SimpleNamespace(close=lambda:None),_active_game_id=lambda:'')
    from jarvis.runtime.atlas2 import PhraseAtlas2
    agent.atlas2 = PhraseAtlas2(cfg, context_memory=agent.context_memory)
    def save(data):
        cfg.save(data)
        cfg.reload()
        Agent.refresh_runtime_settings(agent)
    cancellation_manager().reset()
    return cfg,c,s,save


def alter_saved(cfg,c,save,mode,what,aba):
    before=deepcopy(cfg.data);changed=deepcopy(before)
    if what=='server':key='ollama.base_url';value='http://127.0.0.1:22434'
    elif what=='model':key='local_chat.model' if mode.startswith('text') else 'llm_agent.base_model' if mode.startswith('planner') else 'ollama.vision_model';value='replacement:7b'
    elif what=='role':key='local_chat.enabled' if mode.startswith('text') else 'llm_agent.enabled' if mode.startswith('planner') else 'ollama.vision_enabled';value=False
    elif what=='theme':key='ui.window_opacity';value=.73
    assign(changed,key,value)
    save(changed)
    if aba:save(before)

@pytest.mark.parametrize('mode',MODES)
@pytest.mark.parametrize('what',('server','model','role'))
@pytest.mark.parametrize('aba',(False,True),ids=('one_way','away_and_back'))
def test_saved_settings_transitions_cancel_all_queued_modes(saved_client,mode,what,aba):
    cfg,c,s,save=saved_client
    with patch.object(oc,'_acquire_generation_slot',side_effect=lambda:alter_saved(cfg,c,save,mode,what,aba)),patch.object(oc,'_release_generation_slot'):
        with pytest.raises(OperationCancelled):invoke(c,mode)
    assert not s.generations()

@pytest.mark.parametrize('mode',('text_stream','planner_stream','image_stream'))
@pytest.mark.parametrize('what',('server','model','role'))
@pytest.mark.parametrize('aba',(False,True),ids=('one_way','away_and_back'))
def test_saved_settings_transitions_reject_stream_chunks(saved_client,mode,what,aba):
    cfg,c,s,save=saved_client;seen=[]
    def lines():
        piece='{"reply":"before' if mode.startswith('planner') else 'before'
        yield json.dumps({'message':{'content':piece}})
        alter_saved(cfg,c,save,mode,what,aba)
        piece=' after"}' if mode.startswith('planner') else ' after'
        yield json.dumps({'message':{'content':piece},'done':True})
    s.on_post=lambda url,kw:Response({'modelfile':'FROM base:3b'}) if url.endswith('/api/show') else Response(lines=lines)
    with pytest.raises(OperationCancelled):invoke(c,mode,seen.append)
    assert ''.join(seen)=='before'

@pytest.mark.parametrize('mode',MODES)
def test_theme_only_settings_save_preserves_queued_generation(saved_client,mode):
    cfg,c,s,save=saved_client
    initial=dict(c._model_settings_revisions)
    with patch.object(oc,'_acquire_generation_slot',side_effect=lambda:alter_saved(cfg,c,save,mode,'theme',True)),patch.object(oc,'_release_generation_slot'):
        assert invoke(c,mode)
    assert c._model_settings_revisions==initial and len(s.generations())==1

@pytest.mark.parametrize('mode',('text_stream','planner_stream','image_stream'))
def test_theme_only_settings_save_preserves_stream(saved_client,mode):
    cfg,c,s,save=saved_client;seen=[]
    def lines():
        piece='{"reply":"before' if mode.startswith('planner') else 'before'
        yield json.dumps({'message':{'content':piece}})
        alter_saved(cfg,c,save,mode,'theme',True)
        piece=' after"}' if mode.startswith('planner') else ' after'
        yield json.dumps({'message':{'content':piece},'done':True})
    s.on_post=lambda url,kw:Response({'modelfile':'FROM base:3b'}) if url.endswith('/api/show') else Response(lines=lines)
    assert invoke(c,mode,seen.append)
    assert ''.join(seen)=='before after'


def test_theme_save_during_inventory_does_not_invalidate_it(saved_client):
    cfg,c,s,save=saved_client
    s.on_get=lambda *a:(alter_saved(cfg,c,save,'text','theme',False),Response({'models':[{'name':'chat:0.5b'}]}))[1]
    assert c._list_models()==['chat:0.5b']


def test_real_same_endpoint_old_response_cannot_replace_new_inventory(client):
    cfg,c,s=client;entered=threading.Event();release=threading.Event();errors=[]
    def get(url,kw):
        if threading.current_thread().name=='old-tags':
            entered.set();assert release.wait(2)
            return Response({'models':[{'name':'old:1b'}]})
        return Response({'models':[{'name':'new:1b'}]})
    s.on_get=get
    def work():
        cancellation_manager().reset()
        try:c._list_models()
        except Exception as exc:errors.append(exc)
    t=threading.Thread(target=work,name='old-tags');t.start()
    try:
        assert entered.wait(1)
        c._model_cache=(0,[])
        assert c._list_models(force=True)==['new:1b']
    finally:release.set();t.join(2)
    assert not t.is_alive() and len(errors)==1 and isinstance(errors[0],OperationCancelled)
    assert c._list_models()==['new:1b']

@pytest.mark.parametrize('available',(False,True))
@pytest.mark.parametrize('tuned',('neo-agent-ft','neo-agent-ft:latest','neo-agent-ft-prev'))
def test_custom_model_wins_over_leftover_managed_finetune(client,available,tuned):
    cfg,c,s=client;cfg.values.update({'llm_agent.model':'my-custom:7b','llm_agent.fine_tuned':tuned})
    names=['base:3b','neo-agent:latest','neo-agent-ft:latest']+(['my-custom:7b'] if available else [])
    s.on_get=lambda *a:Response({'models':[{'name':m} for m in names]})
    if available:
        assert c.resolve_agent_model()=='my-custom:7b'
        assert invoke(c,'planner')['_model']=='my-custom:7b'
    else:
        with pytest.raises(oc.OllamaError,match='my-custom:7b'):c.resolve_agent_model()
    assert not any('/api/show' in row[1] for row in s.calls)


def test_preload_uses_endpoint_from_single_model_request_snapshot(client):
    from jarvis.runtime import model_life173 as life
    cfg,c,s=client;capture=c._capture_request;first=True
    def changed_capture(role):
        nonlocal first
        if first:
            first=False;cfg.values.update({'ollama.base_url':'http://127.0.0.1:22434','llm_agent.model':'new-custom:7b'})
        return capture(role)
    c._capture_request=changed_capture
    def get(url,kw):
        return Response({'models':[] if url.endswith('/api/ps') else [{'name':'new-custom:7b'}]})
    s.on_get=get
    life._PRELOAD_INFLIGHT[0]=False;life._LAST_PRELOAD[0]=0
    try:
        assert life.preload(c,background=False,now=100)
        assert len(s.calls)==3 and all('22434' in row[1] for row in s.calls)
        assert s.calls[-1][2]['json']['model']=='new-custom:7b'
    finally:life._PRELOAD_INFLIGHT[0]=False;life._LAST_PRELOAD[0]=0


def test_release_aborts_if_endpoint_and_selection_change_during_capture(client):
    from jarvis.runtime.model_life173 import release
    cfg,c,s=client;candidates=c.agent_model_candidates;changed=False
    def switched_candidates():
        nonlocal changed
        if not changed:
            changed=True
            cfg.values.update({'ollama.base_url':'http://127.0.0.1:22434','llm_agent.model':'new-custom:7b'})
        return candidates()
    c.agent_model_candidates=switched_candidates
    s.on_get=lambda *a:Response({'models':[{'name':'new-custom:7b'}]})
    assert release(c)==0 and s.calls==[]


def test_harmless_concurrent_inventory_refresh_does_not_cancel_question(client):
    cfg,c,s=client;entered=threading.Event();release=threading.Event();errors=[];results=[]
    def get(url,kw):
        if threading.current_thread().name=='question':
            entered.set();assert release.wait(2)
        return Response({'models':[{'name':'chat:0.5b'}]})
    s.on_get=get
    def question():
        cancellation_manager().reset()
        try:results.append(invoke(c,'text'))
        except Exception as exc:errors.append(exc)
    t=threading.Thread(target=question,name='question');t.start()
    try:
        assert entered.wait(1)
        assert c._list_models(force=True)==['chat:0.5b']
    finally:release.set();t.join(2)
    assert not t.is_alive() and not errors and results and len(s.generations())==1
    assert c._list_models()==['chat:0.5b']


def test_out_of_order_harmless_reads_reuse_latest_inventory(client):
    cfg,c,s=client;entered=threading.Event();release=threading.Event();errors=[];results=[]
    def get(url,kw):
        if threading.current_thread().name=='older-read':
            entered.set();assert release.wait(2)
            return Response({'models':[{'name':'old:1b'}]})
        return Response({'models':[{'name':'new:1b'}]})
    s.on_get=get
    def older():
        cancellation_manager().reset()
        try:results.append(c._list_models())
        except Exception as exc:errors.append(exc)
    t=threading.Thread(target=older,name='older-read');t.start()
    try:
        assert entered.wait(1)
        assert c._list_models(force=True)==['new:1b']
    finally:release.set();t.join(2)
    assert not errors and results==[['new:1b']] and c._list_models()==['new:1b']


def test_passive_model_progress_does_not_cancel_inflight_question(client):
    from jarvis.ui.main_window import MainWindow
    cfg,c,s=client;entered=threading.Event();release=threading.Event();errors=[];answers=[]
    bubble=object();updates=[]
    w=SimpleNamespace(_quitting=False,agent=SimpleNamespace(ollama=c),
        _model_progress_bubbles184={'download':bubble},_model_progress_operation184='download',
        chat=SimpleNamespace(update_bubble=lambda target,text:updates.append((target,text))))
    def get(*a):
        entered.set();assert release.wait(2)
        return Response({'models':[{'name':'chat:0.5b'}]})
    s.on_get=get
    def question():
        cancellation_manager().reset()
        try:answers.append(invoke(c,'text'))
        except Exception as exc:errors.append(exc)
    t=threading.Thread(target=question);t.start()
    try:
        assert entered.wait(1)
        MainWindow._model_operation_message(w,'Downloading unrelated model 42%','download')
    finally:release.set();t.join(2)
    assert not errors and answers and len(s.generations())==1 and updates


def test_completed_check_refreshes_inventory_and_conversation_availability(client):
    from jarvis.ui.main_window import MainWindow
    from jarvis.runtime import local_chat169
    cfg,c,s=client
    c._model_cache_endpoint=c.base_url
    c._model_cache=(9999999999.0,[])
    local_chat169._UNAVAILABLE_UNTIL[0]=9999999999.0
    w=SimpleNamespace(_quitting=False,agent=SimpleNamespace(ollama=c),_model_inventory_operations184={'check':c.reserve_model_inventory()})
    MainWindow._model_operation_terminal184(w,{'operation_id':'check','action':'check','base_url':c.base_url,'status':'installed','ready':True,'models':['chat:0.5b']})
    s.on_get=lambda *a:(_ for _ in ()).throw(AssertionError('verified inventory should be reused'))
    assert local_chat169.answer(c,cfg,'hello','en') and len(s.generations())==1
    assert c._model_cache[1]==['chat:0.5b']


def test_readonly_terminal_inventory_does_not_cancel_older_question(client):
    cfg,c,s=client;entered=threading.Event();release=threading.Event();errors=[];answers=[]
    def get(*a):
        entered.set();assert release.wait(2)
        return Response({'models':[]})
    s.on_get=get
    def question():
        cancellation_manager().reset()
        try:answers.append(invoke(c,'text'))
        except Exception as exc:errors.append(exc)
    t=threading.Thread(target=question);t.start()
    try:
        assert entered.wait(1)
        assert c.accept_model_inventory(c.base_url,['chat:0.5b'],sequence=c.reserve_model_inventory())
    finally:release.set();t.join(2)
    assert not errors and answers and len(s.generations())==1

@pytest.mark.parametrize('mutation',(False,True))
def test_late_operation_inventory_never_replaces_newer_completion(client,mutation):
    cfg,c,s=client
    old=c.reserve_model_inventory();new=c.reserve_model_inventory()
    assert c.accept_model_inventory(c.base_url,['new:1b'],sequence=new)
    assert not c.accept_model_inventory(c.base_url,['old:1b'],sequence=old,invalidate=mutation)
    if mutation:
        assert c._model_cache==(0.0,[])
        s.on_get=lambda *a:Response({'models':[{'name':'new:1b'}]})
        assert c._list_models()==['new:1b']
    else:assert c._model_cache[1]==['new:1b']


def test_late_operation_inventory_never_replaces_newer_query(client):
    cfg,c,s=client;old=c.reserve_model_inventory()
    s.on_get=lambda *a:Response({'models':[{'name':'query:1b'}]})
    assert c._list_models(force=True)==['query:1b']
    assert not c.accept_model_inventory(c.base_url,['old:1b'],sequence=old)
    assert c._model_cache[1]==['query:1b']
