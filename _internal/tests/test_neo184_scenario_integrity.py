"""Invalid scenario data and cancelled dialogs remain harmless and recoverable."""
from copy import deepcopy
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QDialog
from tests.test_scenario_workflows184 import Config, item, app, no_actions
from jarvis.runtime import scenarios as runtime
from jarvis.runtime.scenario_schema import validate_scenario
from jarvis.ui.scenario_editor import ScenarioEditor, ScenarioStepDialog

@pytest.mark.parametrize('field,kind', [('seconds','wait'),('monitor','open'),('launch_wait','open'),('timeout','command')])
@pytest.mark.parametrize('value',[10**1000,-10**1000,float('inf'),float('nan')])
def test_extreme_numbers_are_invalid_without_exception(field,kind,value):
    step={'type':kind,'path':'fake.exe','command':['fake.exe'],field:value}
    assert not validate_scenario(item([step])).valid

@pytest.mark.parametrize('bad',['corrupt',None,{},42])
def test_corrupt_steps_cannot_be_appended_or_overwritten(app, monkeypatch, bad):
    raw=item();raw['steps']=bad
    editor=ScenarioEditor(Config([raw]));before=deepcopy(editor.items())
    monkeypatch.setattr(ScenarioStepDialog,'exec',lambda self:pytest.fail('Invalid step container opened an editor'))
    assert not editor.add_step_button.isEnabled()
    editor.add_step()
    assert editor.items()==before
    editor.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

@pytest.mark.parametrize('operation',['add_step','edit_step'])
def test_cancelled_step_dialogs_are_disposed(app,monkeypatch,operation):
    editor=ScenarioEditor(Config([item()]));before=deepcopy(editor.items())
    monkeypatch.setattr(ScenarioStepDialog,'exec',lambda self:QDialog.Rejected)
    for _ in range(15):
        getattr(editor,operation)()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        assert not editor.findChildren(ScenarioStepDialog)
    assert editor.items()==before
    editor.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

@pytest.mark.parametrize('policy,expected_steps',[('stop',1),('continue',2)])
def test_failed_placement_reports_partial_open_and_honors_policy(monkeypatch,policy,expected_steps):
    config=Config([item([{'type':'open','path':'fake.exe','monitor':1,'launch_wait':0,'on_failure':policy},{'type':'wait','seconds':0}])])
    monkeypatch.setattr(runtime,'_open_path',lambda path:{'success':True,'path':path})
    monkeypatch.setattr(runtime,'_move_opened_window',lambda *args:{'moved':False,'error':'monitor unavailable'})
    result=runtime.run_scenario(config,'test-id')
    assert result.status=='failed' and not result.success
    assert len(result.steps)==expected_steps
    assert result.steps[0]['opened'] is True
    assert not result.steps[0]['success']
    assert result.steps[0]['error']=='monitor unavailable'

def test_extreme_scenario_budget_is_invalid():
    assert not validate_scenario(item(max_seconds=10**1000)).valid

@pytest.mark.parametrize('accepted',[False,True])
def test_confirmation_dialog_is_disposed(app,monkeypatch,accepted):
    from PySide6.QtWidgets import QMessageBox
    from jarvis.ui.scenario_editor import _confirm
    editor=ScenarioEditor(Config([item()]))
    def finish(box):
        role=QMessageBox.YesRole if accepted else QMessageBox.RejectRole
        next(b for b in box.buttons() if box.buttonRole(b)==role).click()
        return 0
    monkeypatch.setattr(QMessageBox,'exec',finish)
    for _ in range(8):
        assert _confirm(editor,'en','Delete?') is accepted
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        assert not editor.findChildren(QMessageBox)
    editor.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

def test_accepted_step_is_preserved_after_dialog_disposal(app,monkeypatch):
    editor=ScenarioEditor(Config([item()]))
    def accept(dialog):
        dialog.result_step={'type':'wait','seconds':2}
        return QDialog.Accepted
    monkeypatch.setattr(ScenarioStepDialog,'exec',accept)
    editor.add_step()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
    assert editor.items()[0]['steps'][-1]=={'type':'wait','seconds':2}
    assert not editor.findChildren(ScenarioStepDialog)
    editor.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

@pytest.mark.parametrize('titles,hint,expected',[
    (['Other - Firefox','Wanted - Firefox'],'Wanted','Wanted - Firefox'),
    (['Wanted extra','Wanted'],'Wanted','Wanted'),
    (['Wanted A','Wanted B'],'Wanted',None),
    (['Firefox A','Firefox B'],'',None),
    (['Only Firefox'],'','Only Firefox'),
    (['A','Other Firefox'],'A','A'),
])
def test_window_placement_honors_unique_explicit_target(monkeypatch,titles,hint,expected):
    import sys
    from types import SimpleNamespace
    calls=[]
    class Window:
        isMaximized=False;width=800;height=600
        def __init__(self,title):self.title=title
        def moveTo(self,*args):calls.append(('move',self.title))
        def resizeTo(self,*args):calls.append(('resize',self.title))
    class Screens:
        monitors=[{},dict(width=1920,height=1080,left=0,top=0)]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(runtime,'os',SimpleNamespace(name='nt'))
    monkeypatch.setitem(sys.modules,'mss',SimpleNamespace(mss=Screens))
    monkeypatch.setitem(sys.modules,'pygetwindow',SimpleNamespace(getAllWindows=lambda:[Window(t) for t in titles]))
    result=runtime._move_opened_window('/apps/firefox.exe',1,hint)
    if expected:
        assert result['moved'] and result['title']==expected
        assert calls==[('move',expected),('resize',expected)]
    else:
        assert not result['moved'] and 'Multiple' in result['error']
        assert calls==[]

@pytest.mark.parametrize('language',['ru','en','de','fr','es','it','pt'])
def test_placement_error_is_localized_once_in_log(app,language):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QPushButton,QLabel,QProgressBar,QPlainTextEdit
    from jarvis.ui.scenario_strings import st
    fake=SimpleNamespace(language=language,_matches_event=lambda event:True,_update_run_button=lambda:None,
        cancel_button=QPushButton(),status=QLabel(),progress=QProgressBar(),log=QPlainTextEdit())
    error='Multiple matching windows; specify a unique window title'
    ScenarioEditor._on_event(fake,{'state':'step_finished','success':False,'step_index':1,'total':1,'error':error,'placement':{'error':error}})
    assert fake.log.toPlainText().count(st('window_ambiguous',language))==1
    for widget in (fake.cancel_button,fake.status,fake.progress,fake.log):widget.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
