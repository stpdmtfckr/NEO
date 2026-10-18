"""N38: explicit quality choices and scoped facts, with no external I/O."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QFormLayout
from jarvis.database import Database
from jarvis.runtime import memory169 as memory
from jarvis.runtime.quality_memory_text184 import CODES, ROWS, text
from jarvis.ui.quality_memory184 import FactEditDialog, MemoryFactsDialog, MODEL_CHOICES
from tests.test_neo184_settings_mainwindow import app, window, settle, open_settings, FakeOperation


class Config:
    def __init__(self, code='en', enabled=True):
        self.data={'app': {'language': code}, 'memory': {'enabled': enabled}}
    def get(self,key,default=None):
        value=self.data
        for part in key.split('.'):
            if not isinstance(value,dict) or part not in value:return default
            value=value[part]
        return value

@pytest.fixture
def db(tmp_path):
    value=Database(tmp_path/'facts.sqlite3')
    yield value
    value.close()

def seed(db, key='tea', value='green', name=None):
    db.set_memory('pref:'+key,json.dumps({'name':name or key,'value':value},ensure_ascii=False))
    return next(row for row in memory.user_fact_rows(db) if row['key']=='pref:'+key)


def test_scope_hides_internal_untyped_and_learned_records(db):
    row=seed(db)
    memory.note(db,Config(),'local note','en')
    hidden=['personal_aliases.en','context.last_game','neo_diagnostics','system.secret','internal.token',
            'integrations.api_token','access_token','old untyped fact']
    for key in hidden:db.set_memory(key,'PRIVATE')
    rows=memory.user_fact_rows(db)
    assert len(rows)==2
    assert memory.delete_user_facts(db,rows,'en')[0]
    assert memory.user_fact_rows(db)==[]
    assert all(db.get_memory(key)=='PRIVATE' for key in hidden)
    assert not memory.delete_user_facts(db,db.list_memories(),'en')[0]


def test_edit_reopens_preserves_identity_and_other_rows(db):
    first=seed(db,'tea','green','Tea')
    other=seed(db,'legacy-tea','oolong','Tea')  # same display name, distinct exact key
    assert memory.edit_user_fact(db,Config(),first,'  black  tea! ','en')[0]
    assert db.get_memory(other['key'])==other['value']
    fresh=Database(db.path)
    try:
        stored=next(r for r in memory.user_fact_rows(fresh) if r['key']==first['key'])
        assert json.loads(stored['value'])=={'name':'Tea','value':'black tea'}
        assert memory.delete_user_facts(fresh,[stored],'en')[0]
    finally:fresh.close()
    assert db.get_memory(first['key'])=='' and db.get_memory(other['key'])==other['value']


@pytest.mark.parametrize('raw',['{broken','[]','null','42','"text"','{"value":"visible"}'])
def test_corrupt_preference_rows_can_be_viewed_repaired_and_deleted(db,raw):
    db.set_memory('pref:broken',raw)
    row=memory.user_fact_rows(db)[0]
    assert memory._decode(row)[0]=='pref'
    assert memory.edit_user_fact(db,Config(),row,'repaired','en')[0]
    row=memory.user_fact_rows(db)[0]
    assert json.loads(row['value'])['value']=='repaired'
    assert memory.delete_user_facts(db,[row],'en')[0]


@pytest.mark.parametrize('kind,limit',[('pref',memory.VALUE_MAX),('note',memory.NOTE_MAX)])
def test_shared_validation_never_truncates_and_disabled_memory_refuses_save(db,kind,limit):
    config=Config()
    if kind=='pref':row=seed(db)
    else:
        memory.note(db,config,'a note','en');row=memory.user_fact_rows(db)[0]
    assert not memory.edit_user_fact(db,config,row,'X'*(limit+1),'en')[0]
    assert db.get_memory(row['key'])==row['value']
    assert not memory.edit_user_fact(db,config,row,'   ','en')[0]
    config.data['memory']['enabled']=False
    assert not memory.edit_user_fact(db,config,row,'changed','en')[0]
    assert memory.delete_user_facts(db,[row],'en')[0]  # deletion is permitted while memory is off


def test_stale_edit_and_bulk_delete_are_atomic(db):
    first=seed(db,'first');second=seed(db,'second')
    db.set_memory(second['key'],'changed elsewhere')
    assert not memory.edit_user_fact(db,Config(),second,'overwrite','en')[0]
    assert not memory.delete_user_facts(db,[first,second],'en')[0]
    assert db.get_memory(first['key'])==first['value']
    assert db.get_memory(second['key'])=='changed elsewhere'
    assert not memory.delete_user_facts(db,[first,first],'en')[0]
    assert db.get_memory(first['key'])==first['value']


@pytest.mark.parametrize('action',['edit','bulk'])
def test_commit_failure_rolls_back_and_later_write_cannot_commit_failed_changes(db,action):
    first=seed(db,'first');second=seed(db,'second')
    connection=db._db()
    class FailingCommit:
        def __getattr__(self,key):return getattr(connection,key)
        def commit(self):raise OSError('disk full: PRIVATE PATH')
    db._connection=FailingCommit()
    with pytest.raises(OSError):
        if action=='edit':memory.edit_user_fact(db,Config(),first,'new','en')
        else:memory.delete_user_facts(db,[first,second],'en')
    db._connection=connection
    db.set_memory('pref:unrelated','unchanged transaction')
    assert db.get_memory(first['key'])==first['value']
    assert db.get_memory(second['key'])==second['value']


def test_ui_save_cancel_failure_retry_and_reopen(app,db,monkeypatch):
    config=Config();row=seed(db)
    dialog=FactEditDialog(db,config,row)
    dialog.value.setPlainText('cancelled');dialog.reject()
    assert db.get_memory(row['key'])==row['value']
    dialog=FactEditDialog(db,config,row)
    dialog.value.setPlainText('X'*501);dialog.save()
    assert dialog.result()!=QDialog.Accepted and dialog.value.toPlainText()=='X'*501
    assert '501' in dialog.status.text()
    dialog.value.setPlainText('retry value')
    original=db.change_memory_snapshot
    monkeypatch.setattr(db,'change_memory_snapshot',lambda *a,**k:(_ for _ in ()).throw(OSError('secret')))
    dialog.save()
    assert dialog.result()!=QDialog.Accepted and dialog.save_button.isEnabled()
    assert 'secret' not in dialog.status.text()
    assert dialog.value.toPlainText()=='retry value'
    monkeypatch.setattr(db,'change_memory_snapshot',original)
    dialog.save();assert dialog.result()==QDialog.Accepted
    reopened=MemoryFactsDialog(db,config)
    assert reopened.table.item(0,1).text()=='retry value'
    dialog.deleteLater();reopened.deleteLater()


def test_ui_delete_confirmation_cancel_bulk_scope_and_empty_state(app,db,monkeypatch):
    seed(db,'a');seed(db,'b');db.set_memory('personal_aliases.en','keep')
    config=Config(enabled=False);dialog=MemoryFactsDialog(db,config)
    dialog.table.selectRow(0)
    assert not dialog.edit_button.isEnabled() and dialog.delete_button.isEnabled()
    requests=[]
    def cancel(box):
        requests.append(box.text());assert box.defaultButton()==box.button(QMessageBox.No)
        return QMessageBox.No
    monkeypatch.setattr(QMessageBox,'exec',cancel)
    dialog.delete_all()
    assert len(memory.user_fact_rows(db))==2 and '(2)' in requests[0]
    assert 'settings will remain' in requests[0]
    monkeypatch.setattr(QMessageBox,'exec',lambda box:QMessageBox.Yes)
    dialog.delete_all()
    assert not dialog.rows and not dialog.clear_button.isEnabled()
    assert db.get_memory('personal_aliases.en')=='keep'
    dialog.deleteLater()


def test_ui_read_failure_keeps_existing_rows_without_allowing_mutation(app,db,monkeypatch):
    seed(db);dialog=MemoryFactsDialog(db,Config());dialog.table.selectRow(0)
    original=memory.user_fact_rows
    monkeypatch.setattr(memory,'user_fact_rows',lambda db:(_ for _ in ()).throw(OSError('secret')))
    assert not dialog.reload()
    assert dialog.table.rowCount()==1 and not dialog.edit_button.isEnabled()
    assert not dialog.delete_button.isEnabled() and 'secret' not in dialog.status.text()
    monkeypatch.setattr(memory,'user_fact_rows',original)
    assert dialog.reload() and len(dialog.rows)==1
    dialog.deleteLater()


def test_ui_stale_delete_keeps_rows_and_ignores_duplicate_activation(app,db,monkeypatch):
    row=seed(db);dialog=MemoryFactsDialog(db,Config());dialog.table.selectRow(0)
    count=[]
    def confirm(box):
        count.append(1);dialog.delete_all()  # nested modal event reentry must not ask twice
        db.set_memory(row['key'],'another writer')
        return QMessageBox.Yes
    monkeypatch.setattr(QMessageBox,'exec',confirm)
    dialog.delete_selected()
    assert count==[1] and len(dialog.rows)==1 and 'Refresh' in dialog.status.text()
    assert db.get_memory(row['key'])=='another writer'
    dialog.deleteLater()


@pytest.mark.parametrize('code',CODES)
def test_all_editor_languages_and_unmodified_user_text(app,db,code):
    for key,row in ROWS.items():
        assert len(row)==7 and all(row)
        text(key,code,count=2,limit=500)
    seed(db,'opaque','<b>Keep русский café</b>','Tea & <x>')
    config=Config(code);dialog=MemoryFactsDialog(db,config)
    assert dialog.windowTitle()==text('facts',code)
    assert dialog.edit_button.text()==text('edit',code)
    assert dialog.table.item(0,1).text()=='<b>Keep русский café</b>'
    editor=FactEditDialog(db,config,dialog.rows[0],dialog)
    editor.value.setPlainText('Новый value');editor.save()
    assert json.loads(db.get_memory('pref:opaque'))['value']=='Новый value'
    editor.deleteLater();dialog.deleteLater()


def test_disabled_pending_switch_cannot_be_bypassed(app,db):
    row=seed(db);enabled=[True];config=Config()
    editor=FactEditDialog(db,config,row,edits_enabled=lambda:enabled[0])
    enabled[0]=False;editor.value.setPlainText('refused');editor.save()
    assert editor.result()!=QDialog.Accepted and db.get_memory(row['key'])==row['value']
    editor.deleteLater()


def test_quality_preserves_custom_selection_inventory_refresh_and_cancel(window,app,monkeypatch):
    window.config.data['local_chat']['model']='my-trained-custom:latest'
    window.config.data['llm_agent']['fine_tuned']='another-trained:latest'
    d=open_settings(window,app)
    assert d.chat_model.currentText()=='my-trained-custom:latest'
    assert all(d.chat_model.findText(tag)>=0 for tag in MODEL_CHOICES)
    d._model_completed184({'models':['installed-other:7b'],'message':'ok'})
    assert d.chat_model.currentText()=='my-trained-custom:latest'
    assert all(d.chat_model.findText(tag)>=0 for tag in MODEL_CHOICES)
    d._model_completed184({'models':['my-trained-custom:latest'],'message':'ok'})
    for model,key in MODEL_CHOICES.items():
        assert d.chat_model.itemData(d.chat_model.findText(model),Qt.ToolTipRole)==text(key,'ru')
    d.chat_model.setCurrentText('qwen2.5:3b')
    assert window.config.get('local_chat.model')=='my-trained-custom:latest'
    d.reject();settle(app)
    d=open_settings(window,app)
    assert d.chat_model.currentText()=='my-trained-custom:latest'
    assert window.config.get('llm_agent.fine_tuned')=='another-trained:latest'
    d.reject()


def test_quality_actual_apply_persistence_runtime_refresh_no_download(window,app,monkeypatch):
    from jarvis.config import ConfigManager
    from jarvis.ui.settings_dialog import QMessageBox as SettingsMessages
    from jarvis.model_management import ModelOperation
    monkeypatch.setattr(ModelOperation,'start',lambda self:pytest.fail('No download/start on selection or Apply'))
    monkeypatch.setattr(window.audio_capture,'restart_backend',lambda:None)
    if getattr(window,'desktop_services',None):monkeypatch.setattr(window.desktop_services,'reconfigure',lambda:None)
    monkeypatch.setattr(window,'_configure_mail_monitor_timer',lambda:None)
    errors=[];monkeypatch.setattr(SettingsMessages,'critical',lambda *a:errors.append(a))
    d=open_settings(window,app)
    assert d.chat_model.currentText()=='qwen2.5:0.5b'
    with patch.object(QTimer,'singleShot',lambda *a,**k:None):d._save(close=False)
    revision=window.agent.ollama._model_settings_revisions['chat']
    d.chat_model.setCurrentText('qwen2.5:7b')
    assert window.config.get('local_chat.model')=='qwen2.5:0.5b'
    with patch.object(QTimer,'singleShot',lambda *a,**k:None):d._save(close=False)
    assert not errors
    assert window.config.get('local_chat.model')=='qwen2.5:7b'
    assert window.config.get('ollama.chat_model')=='qwen2.5:7b'
    assert window.agent.ollama._model_settings_revisions['chat']>revision
    persisted=json.loads(window.config.paths.settings.read_text(encoding='utf-8-sig'))
    assert persisted['local_chat']['model']=='qwen2.5:7b'
    monkeypatch.setattr(window.agent.ollama,'_list_models',lambda:['qwen2.5:0.5b','qwen2.5:7b','neo-agent:latest'])
    assert window.agent.ollama.resolve_chat_model()=='qwen2.5:7b'
    d.reject();settle(app);d=open_settings(window,app)
    assert d.chat_model.currentText()=='qwen2.5:7b'
    # A real ConfigManager.update made while Settings is open must survive Apply.
    window.config.update(lambda data:data['llm_agent'].update(fine_tuned='my-trained-command:latest'))
    d.chat_model.setCurrentText('my-custom-chat:latest')
    with patch.object(QTimer,'singleShot',lambda *a,**k:None):d._save(close=False)
    assert window.config.get('local_chat.model')=='my-custom-chat:latest'
    assert window.config.get('llm_agent.fine_tuned')=='my-trained-command:latest'
    assert not errors
    d.reject()


def test_quality_existing_download_check_delete_paths_use_exact_selection(window,app,monkeypatch):
    import jarvis.model_management as service
    from jarvis.ui.settings_dialog import QMessageBox as SettingsMessages
    monkeypatch.setattr(service,'ModelOperation',FakeOperation)
    monkeypatch.setattr(SettingsMessages,'question',lambda *a:QMessageBox.Yes)
    d=open_settings(window,app);before=deepcopy(window.config.data);d.chat_model.setCurrentText('qwen2.5:3b')
    for action,button in [('check',d.chat_model_check184),('download',d.chat_model_download184),('delete',d.chat_model_delete184)]:
        button.click();op=d._model_operation184
        assert op.model=='qwen2.5:3b' and op.action==action
        op.completed.emit({'status':'installed','message':'ok','models':['qwen2.5:3b']});op.finished.emit();settle(app)
    assert window.config.data==before
    d.reject()

@pytest.mark.parametrize('code',CODES)
def test_quality_language_guidance_with_fake_hardware_and_no_automatic_change(app,code,monkeypatch):
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QComboBox
    from jarvis.ui.quality_memory184 import append_quality_choices
    from jarvis.i18n import translate_widget_tree
    import psutil
    monkeypatch.setattr(psutil,'virtual_memory',lambda:SimpleNamespace(total=2*1024**3,available=256*1024**2))
    class Host(QDialog):
        settings_saved=Signal()
    host=Host();host.config=Config(code);host.chat_model=QComboBox();host.chat_model.setEditable(True)
    host.chat_model.addItem('my-trained:latest');form=QFormLayout(host);form.addRow(host.chat_model)
    append_quality_choices(host,form);translate_widget_tree(host,code)
    assert host.chat_model.currentText()=='my-trained:latest'
    assert host.quality_help184.text()==text('custom',code)
    for model,key in MODEL_CHOICES.items():
        host.chat_model.setCurrentText(model)
        assert host.quality_help184.text()==text(key,code)
        assert host.chat_model.itemData(host.chat_model.findText(model),Qt.ToolTipRole)==text(key,code)
    # Low RAM is guidance, never a reason for the UI to silently switch a model.
    assert host.chat_model.currentText()=='qwen2.5:7b'
    assert host.config.get('local_chat.model') is None
    host.deleteLater()


def test_quality_apply_failure_keeps_staged_choice_without_mutation(window,app,monkeypatch):
    from jarvis.ui.settings_dialog import QMessageBox as SettingsMessages
    d=open_settings(window,app);before=deepcopy(window.config.data)
    d.chat_model.setCurrentText('qwen2.5:1.5b')
    errors=[];monkeypatch.setattr(SettingsMessages,'critical',lambda *a:errors.append(a))
    monkeypatch.setattr(window.config,'save',lambda *a,**k:(_ for _ in ()).throw(OSError('inert disk failure')))
    d._save(close=False)
    assert errors and window.config.data==before
    assert d.chat_model.currentText()=='qwen2.5:1.5b'
    d.reject()


def test_full_settings_memory_button_uses_active_database_and_close_cancels_draft(window,app,monkeypatch):
    from jarvis.ui.quality_memory184 import MemoryFactsDialog
    row=seed(window.agent.database)
    d=open_settings(window,app);called=[]
    def inspect(editor):
        called.append(editor.database is window.agent.database)
        assert editor.rows[0]['key']==row['key']
        editor.table.selectRow(0)
        assert editor.edit_button.isEnabled()
        draft=FactEditDialog(editor.database,editor.config,editor.rows[0],editor)
        draft.value.setPlainText('uncommitted');draft.reject();draft.deleteLater()
        return QDialog.Rejected
    monkeypatch.setattr(MemoryFactsDialog,'exec',inspect)
    d.memory_editor_button184.click()
    assert called==[True] and window.agent.database.get_memory(row['key'])==row['value']
    d.reject()


@pytest.mark.parametrize('code',CODES)
def test_editor_narrow_layout_and_optional_screenshot(app,db,code):
    import os
    from pathlib import Path
    seed(db,'name','Alex');seed(db,'drink','Tea')
    memory.note(db,Config(code),'Buy groceries on Friday','en')
    dialog=MemoryFactsDialog(db,Config(code));dialog.resize(480,500);dialog.show();settle(app)
    assert dialog.table.width()>200 and dialog.table.height()>70
    buttons=[dialog.edit_button,dialog.delete_button,dialog.refresh_button,dialog.clear_button]
    for button in buttons:
        assert button.width()>=button.minimumSizeHint().width()
    output=os.environ.get('NEO_SCREENSHOT_DIR')
    if output:
        Path(output).mkdir(parents=True,exist_ok=True)
        assert dialog.grab().save(str(Path(output)/f'facts-{code}.png'))
    dialog.reject();dialog.deleteLater()


def test_editor_inherits_actual_settings_style_and_quality_page(window,app,monkeypatch):
    import os
    from pathlib import Path
    window.config.data['app']['language']='en'
    seed(window.agent.database,'preferred tea','Green tea')
    seed(window.agent.database,'first name','Alex')
    d=open_settings(window,app);d.resize(1000,740);d.chat_model.setCurrentText('qwen2.5:3b');settle(app)
    editor=MemoryFactsDialog(window.agent.database,window.config,d)
    editor.show();editor.table.selectRow(0);settle(app)
    assert editor.parent() is d and d.styleSheet()
    output=os.environ.get('NEO_SCREENSHOT_DIR')
    if output:
        Path(output).mkdir(parents=True,exist_ok=True)
        assert d.grab().save(str(Path(output)/'quality-settings-en.png'))
        assert editor.grab().save(str(Path(output)/'facts-settings-en.png'))
    editor.reject();editor.deleteLater();d.reject()


def test_standalone_editor_never_prunes_history(app,db,monkeypatch):
    from PySide6.QtWidgets import QCheckBox
    from jarvis.ui.quality_memory184 import open_memory_editor
    seed(db)
    connection=db._db()
    connection.executemany('INSERT INTO action_log(created_at,tool_name,arguments_json,risk,success,result_text) VALUES(?,?,?,?,?,?)',
                          [('now','test','{}','low',1,'keep') for _ in range(5010)])
    connection.commit()
    config=Config();config.paths=SimpleNamespace(database=db.path)
    host=QDialog();host.config=config;host.memory_enabled169=QCheckBox();host.memory_enabled169.setChecked(True)
    def inspect(editor):
        assert editor.rows and editor.database is not db
        return QDialog.Rejected
    monkeypatch.setattr(MemoryFactsDialog,'exec',inspect)
    open_memory_editor(host)
    assert connection.execute('SELECT COUNT(*) FROM action_log').fetchone()[0]==5010
    host.deleteLater()
