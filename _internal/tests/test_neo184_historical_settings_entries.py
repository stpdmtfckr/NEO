import os,json,hashlib,time
from pathlib import Path
from types import SimpleNamespace,MethodType
from unittest.mock import patch
import pytest
from PySide6.QtCore import Qt,QTimer,QCoreApplication,QEvent,QPoint,QThreadPool
from PySide6.QtWidgets import QApplication,QWidget,QLabel,QPushButton,QCheckBox,QStyleOptionButton,QStyle
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from jarvis.ui.main_window import MainWindow
ORIGINAL_RESUME=MainWindow._resume_wake_listening
from tests.test_neo184_settings_mainwindow import app,window,settle,FakeOperation,open_settings
from tests.test_neo184_settings import isolated,dialog
from jarvis.ui.qt_localization import install_ui_language
BASE=Path(os.environ.get('NEO_CANDIDATE_ROOT', str(Path(__file__).resolve().parents[2])))
import tempfile
_OUTPUT_TMP = tempfile.TemporaryDirectory(prefix='neo-settings-qa-') if not os.environ.get('NEO_EVIDENCE_OUT') else None
OUT=Path(os.environ.get('NEO_EVIDENCE_OUT') or _OUTPUT_TMP.name)
OUT.mkdir(parents=True,exist_ok=True)
FACTOR=os.environ.get('QT_SCALE_FACTOR','1')
FILES=['_internal/jarvis/ui/settings_dialog.py','_internal/jarvis/ui/main_window.py','_internal/jarvis/ui/scenario_confirmation184.py','_internal/jarvis/ui/responsive178.py','_internal/jarvis/ui/qt_localization.py','_internal/jarvis/integrations/instant_replay169.py','_internal/jarvis/ui/settings_text184.py']
HASHES={p:hashlib.sha256((BASE/p).read_bytes()).hexdigest() for p in FILES}
(OUT/f'hashes_{FACTOR}.json').write_text(json.dumps(HASHES,indent=2))
def save(name,data):
    (OUT/f'{name}_{FACTOR}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2));print(name,json.dumps(data,ensure_ascii=False))
def shot(w,name):
    p=OUT/f'{name}_{FACTOR}.png';w.grab().save(str(p));return str(p)
def turn(app,ms=60):QTest.qWait(ms);app.processEvents()
def page(d,w):
    return next((i,d.tabs._stack.widget(i)) for i in range(d.tabs._stack.count()) if d.tabs._stack.widget(i).isAncestorOf(w))
def geometry(a):
    bad=[]
    for w in a.widget().findChildren(QWidget):
        if w.isHidden():continue
        if isinstance(w,QLabel) and w.wordWrap() and w.heightForWidth(w.width())>w.height()+1:
            bad.append({'type':'label_height','text':w.text(),'height':w.height(),'need':w.heightForWidth(w.width())})
        if isinstance(w,QPushButton):
            o=QStyleOptionButton();w.initStyleOption(o);r=w.style().subElementRect(QStyle.SE_PushButtonContents,o,w)
            if w.fontMetrics().horizontalAdvance(w.text())>r.width():bad.append({'type':'button_width','text':w.text(),'width':r.width()})
    return {'horizontal':a.horizontalScrollBar().maximum(),'bad':bad,'content_height':a.widget().height()}
def test_discord_search_enable_and_unsupported_nvidia(dialog,app,monkeypatch):
    d=dialog;d.show();d.resize(600,420);turn(app)
    d.tabs._search.setText('Discord');turn(app,200);d.tabs._search.returnPressed.emit();turn(app)
    result={'discord':[]}
    for enabled in [True,False,True]:
        d.discord_integration_enabled.setChecked(enabled);turn(app,100)
        i,a=page(d,d.discord_integration_enabled);d.tabs.setCurrentIndex(i);a.ensureWidgetVisible(d.discord_integration_enabled,10,10);turn(app)
        boxes=[w for w in a.widget().findChildren(QCheckBox) if not w.isHidden()]
        overlaps=[]
        for j,x in enumerate(boxes):
            for y in boxes[j+1:]:
                if x.parentWidget() is y.parentWidget() and x.geometry().intersects(y.geometry()):overlaps.append([x.text(),y.text()])
        result['discord'].append({'enabled':enabled,**geometry(a),'overlaps':overlaps,'screenshot':shot(d,'discord_'+str(enabled)+'_'+str(len(result['discord'])))})
    d.tabs._search.clear();turn(app,200)
    from jarvis.integrations.instant_replay169 import InstantReplay,message
    original_init = InstantReplay.__init__
    monkeypatch.setattr(InstantReplay,'__init__',lambda self,config: original_init(self,config,is_windows=False))
    i,a=page(d,d.replay_check184);d.tabs.setCurrentIndex(i);a.ensureWidgetVisible(d.replay_check184,10,10);turn(app)
    d.replay_check184.click();turn(app,200)
    result['unsupported']={'shown':d.replay_state169.text(),'expected':message('unsupported','ru'),'screenshot':shot(d,'nvidia_unsupported')}
    save('discord_nvidia',result)
def test_long_model_italian_apply(dialog,app,monkeypatch):
    import jarvis.model_management as service
    from jarvis.i18n import translate_widget_tree
    monkeypatch.setattr(service,'ModelOperation',FakeOperation)
    d=dialog;d.show();d.resize(600,420);d.app_language.setCurrentIndex(d.app_language.findData('it'));d._save(close=False);turn(app)
    i,a=page(d,d.chat_model);d.tabs.setCurrentIndex(i);turn(app)
    long='custom/very-long-local-model-name-'+('quality-'*17)+':7b'
    rows=[]
    def check(model,label):
        d.chat_model.setCurrentText(model);a.ensureWidgetVisible(d.chat_model_check184,10,10);d.chat_model_check184.click();op=d._model_operation184
        assert op is not None and op.started
        op.completed.emit({'status':'installed','message':f'Модель {model} установлена.','model':model,'models':[model,'qwen2.5:0.5b']});op.finished.emit();turn(app)
        rows.append({'label':label,'model':d.chat_model.currentText(),'status':d.chat_model_status184.text(),'minheight':a.widget().minimumHeight(),'sizehintheight':a.widget().sizeHint().height(),'dpr':d.devicePixelRatioF(),**geometry(a),'screenshot':shot(d,'model_'+label)})
    check('qwen2.5:0.5b','short_before');check(long,'long')
    original={id(w):(w,w.text()) for w in a.widget().findChildren(QLabel) if w.property('i18n_text_source')}
    d._save(close=False);turn(app)
    after_apply_status=d.chat_model_status184.text()
    original={id(w):(w,w.text()) for w in a.widget().findChildren(QLabel) if w.property('i18n_text_source')}
    translate_widget_tree(d,'it');translate_widget_tree(d,'it');turn(app)
    changes=[{'before':old,'after':w.text(),'source':w.property('i18n_text_source')} for w,old in original.values() if w.text()!=old]
    check('qwen2.5:0.5b','short_after')
    for size in [(1100,740),(600,420),(900,650),(600,420)]:d.resize(*size);turn(app)
    rows.append({'label':'reverse_final','model':d.chat_model.currentText(),'minheight':a.widget().minimumHeight(),'sizehintheight':a.widget().sizeHint().height(),**geometry(a),'screenshot':shot(d,'model_reverse_final')})
    save('long_model_italian',{'rows':rows,'changed_on_repeat_translation':changes,'after_apply_status':after_apply_status,'errors':d._test_errors184})
def test_disabled_wake_original_entries(window,app,monkeypatch):
    import numpy as np
    from jarvis.voice import RecordingResult
    from jarvis.agent_types import AgentTurn
    w=window;calls=[]
    monkeypatch.setattr(w,'_resume_wake_listening',MethodType(ORIGINAL_RESUME,w))
    monkeypatch.setattr(w.audio_capture,'start_wake_monitor',lambda **kw:calls.append('wake_start'))
    monkeypatch.setattr(w.audio_capture,'restart_backend',lambda:None)
    monkeypatch.setattr(w.audio_capture,'stop',lambda *a,**k:None)
    monkeypatch.setattr(w,'_restore_listening_audio',lambda:None)
    monkeypatch.setattr(w,'_start_interrupt_wake_monitor',lambda *_:None)
    monkeypatch.setattr(w,'_configure_mail_monitor_timer',lambda:None)
    if getattr(w,"desktop_services",None) is not None:monkeypatch.setattr(w.desktop_services,'reconfigure',lambda:None)
    monkeypatch.setattr(w,'_run_agent_stream',lambda text,generation,*a,**kw:(generation,AgentTurn('QA answer.')))
    rows=[]
    for kind in ['disabled_mic','app_muted','wake_disabled']:
        w._voice_mode='';w._continuous_voice_session=False;w._busy=False
        w._app_mic_muted=kind=='app_muted';w.config.data['voice']['enabled']=kind!='disabled_mic';w.config.data['wake_word']['enabled']=kind!='wake_disabled';w.config.data['wake_word']['phrase']='NEO'
        w.config.data['ui']['character_streaming_enabled']=False
        d=open_settings(w,app);d.voice_enabled.setChecked(kind!='disabled_mic');d.wake_enabled.setChecked(kind!='wake_disabled');d.character_streaming_enabled.setChecked(False)
        with patch.object(QTimer,'singleShot',lambda *_a,**_kw:None):d._save(close=False)
        turn(app);row={'kind':kind,'after_apply':w.status.text(),'calls_after_apply':list(calls)}
        d.reject();turn(app,300);row['after_close']=w.status.text();row['calls_after_close']=list(calls)
        w._send_text('qa original entry request');turn(app,350);row['after_text']=w.status.text();row['calls_after_text']=list(calls)
        w._voice_mode='command';w._audio_finished(RecordingResult(np.zeros(0,dtype=np.float32),16000,empty_reason='cancelled'));turn(app,100)
        row['after_cancel_capture']=w.status.text();row['calls_after_cancel']=list(calls);row['screenshot']=shot(w,'status_'+kind);rows.append(row);calls.clear()
    save('disabled_wake',rows)
def luminance(color):
    vals=[v/255 for v in [color.red(),color.green(),color.blue()]]
    vals=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in vals]
    return sum(a*b for a,b in zip(vals,[.2126,.7152,.0722]))
def contrast(a,b):
    x,y=sorted([luminance(a),luminance(b)]);return (y+.05)/(x+.05)
def test_scenario_actual_confirmation_themes_escape_close(window,app,monkeypatch):
    from jarvis.ui.theme_presets import THEME_PRESETS
    from jarvis.ui.scenario_confirmation184 import ScenarioConfirmationDialog
    from jarvis.security import Risk
    w=window;calls=[];rows=[];errors=[]
    w.config.data.setdefault('security',{})['confirm_medium_risk']=True
    w.config.data['scenarios']['items']=[{'id':'qa-confirm','name':'QA Theme','phrase':'qa theme','enabled':True,'steps':[{'type':'wait','seconds':0}]}]
    monkeypatch.setattr(w.agent.tools,'risk_for',lambda *a:Risk.MEDIUM)
    monkeypatch.setattr(w.agent.tools,'execute',lambda *a:calls.append(a))
    for index,(theme,values) in enumerate(THEME_PRESETS.items()):
        w.config.data['ui'].update(values['window']);w._apply_settings();turn(app)
        mode='escape' if index%2==0 else 'close'
        def inspect():
            box=app.activeModalWidget()
            try:
                assert isinstance(box,ScenarioConfirmationDialog)
                pal=box.preview.palette();fg=pal.color(QPalette.Text);bg=pal.color(QPalette.Base)
                rows.append({'theme':theme,'mode':mode,'fg':fg.name(),'bg':bg.name(),'contrast':contrast(fg,bg),'default_cancel':box.cancel.isDefault(),'approve_text':box.approve.text(),'cancel_text':box.cancel.text(),'screenshot':shot(box,'scenario_'+str(index))})
            except BaseException as ex:errors.append(str(ex))
            finally:
                if box is not None:
                    if mode=='escape':QTest.keyClick(box,Qt.Key_Escape)
                    else:box.close()
        QTimer.singleShot(0,inspect);w._run_shelf_scenario('qa-confirm');QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);turn(app)
        assert not calls
    save('scenario_themes',{'rows':rows,'errors':errors,'tool_calls':calls,'remaining_dialogs':len(w.findChildren(ScenarioConfirmationDialog))})
