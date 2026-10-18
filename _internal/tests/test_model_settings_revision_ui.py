"""Actual SettingsDialog Save -> MainWindow -> Agent model revision integration."""
from unittest.mock import patch
import json
import time
import pytest
from PySide6.QtCore import QTimer
import jarvis.ollama_client as oc
from jarvis.runtime.cancellation import OperationCancelled,cancellation_manager
from tests.test_neo184_settings_mainwindow import app,window,settle
if __package__:
    from .test_model_request_fixes import Session,Response,invoke
else:
    from test_model_request_fixes import Session,Response,invoke

def open_settings(window, app):
    window._open_settings()
    dialog = window._settings_dialog
    deadline = time.monotonic() + 15
    while not dialog._settings_ready160 and not dialog._settings_build_error160 and time.monotonic() < deadline:
        settle(app, .015)
    assert not dialog._settings_build_error160, dialog._settings_build_error160
    assert dialog._settings_ready160
    dialog.tabs.setCurrentIndex(8)
    return dialog

@pytest.mark.parametrize('stage',('queue','stream'))
@pytest.mark.parametrize('change',('server','model','theme'))
def test_actual_settings_save_path_remembers_transitions(window,app,monkeypatch,stage,change):
    from jarvis.ui.settings_dialog import QMessageBox
    c=window.agent.ollama;s=Session();c._session=s
    monkeypatch.setattr(oc.requests,'Session',lambda:s)
    monkeypatch.setattr(c,'resolve_chat_model',lambda:str(window.config.get('local_chat.model','') or window.config.get('ollama.chat_model','qwen2.5:0.5b')))
    monkeypatch.setattr(c,'_runner_resource_options',lambda:{})
    monkeypatch.setattr(window.audio_capture,'restart_backend',lambda:None)
    if getattr(window,'desktop_services',None) is not None:monkeypatch.setattr(window.desktop_services,'reconfigure',lambda:None)
    monkeypatch.setattr(window,'_configure_mail_monitor_timer',lambda:None)
    failures=[]
    monkeypatch.setattr(QMessageBox,'critical',lambda *args:failures.append(str(args[-1])))
    d=open_settings(window,app)
    # First normal Save may normalize legacy defaults. Start the tested request
    # after that baseline, so the theme-only control isolates appearance changes.
    with patch.object(QTimer,'singleShot',lambda *a,**kw:None):d._save(close=False)
    assert not failures
    start_revision=c._model_settings_revisions['chat']
    if change=='server':field=d.ollama_url;old=field.text();set_value=field.setText;other='http://127.0.0.1:22434'
    elif change=='model':field=d.chat_model;old=field.currentText();set_value=field.setCurrentText;other='replacement:7b'
    else:field=d.window_background_opacity;old=field.value();set_value=field.setValue;other=73 if old!=73 else 74
    saves=[]
    d.settings_saved.connect(lambda:saves.append(c._model_settings_revisions['chat']))
    def save_away_back():
        with patch.object(QTimer,'singleShot',lambda *a,**kw:None):
            set_value(other);d._save(close=False)
            set_value(old);d._save(close=False)
        assert not failures and len(saves)==2
    cancellation_manager().reset();seen=[]
    if stage=='queue':
        scope=patch.object(oc,'_acquire_generation_slot',side_effect=save_away_back)
        with scope,patch.object(oc,'_release_generation_slot'):
            if change=='theme':assert invoke(c,'text')
            else:
                with pytest.raises(OperationCancelled):invoke(c,'text')
        assert len(s.generations())==(1 if change=='theme' else 0)
    else:
        def lines():
            yield json.dumps({'message':{'content':'before'}})
            save_away_back()
            yield json.dumps({'message':{'content':' after'},'done':True})
        s.on_post=lambda *a:Response(lines=lines)
        if change=='theme':assert invoke(c,'text_stream',seen.append)
        else:
            with pytest.raises(OperationCancelled):invoke(c,'text_stream',seen.append)
        assert ''.join(seen)==('before after' if change=='theme' else 'before')
    if change=='theme':assert c._model_settings_revisions['chat']==start_revision
    else:assert c._model_settings_revisions['chat']>=start_revision+2
    d.reject();settle(app)
