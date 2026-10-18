"""Actual GUI background dispatch must not pass unsupported kwargs to callbacks."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from PySide6.QtWidgets import QApplication

@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])

class Pool:
    def __init__(self):self.errors=[];self.results=[]
    def start(self,worker):
        worker.signals.error.connect(self.errors.append)
        worker.signals.result.connect(self.results.append)
        worker.run()


def fake():
    return SimpleNamespace(_quitting=False,agent=SimpleNamespace(ollama=None),config=SimpleNamespace(get=lambda k,d=None:d),_language_code=lambda:'ru',thread_pool=Pool(),_append_assistant=lambda x:None)


def test_phrase_warmup_really_runs(app):
    from jarvis.ui.main_window import MainWindow
    w=fake();calls=[]
    with patch('jarvis.runtime.speed174.warm_up',side_effect=lambda a,c:calls.append((a,c))):MainWindow._warm_up174(w)
    assert calls==[(w.agent,'ru')] and w.thread_pool.errors==[]


def test_startup_field_check_really_runs(app):
    from jarvis.ui.main_window import MainWindow
    w=fake()
    with patch('jarvis.runtime.fieldkit173.startup_problems',return_value=[]) as check,patch('jarvis.runtime.fieldkit173.not_told_recently',return_value=[]),patch('jarvis.runtime.fieldkit173.problems_text',return_value=''):
        MainWindow._startup_selfcheck173(w)
    check.assert_called_once();assert w.thread_pool.errors==[]


def test_weekly_report_really_runs(app):
    from jarvis.ui.main_window import MainWindow
    w=fake()
    with patch('jarvis.runtime.learning172.weekly_due',return_value=True),patch('jarvis.runtime.learning172.report_text',return_value='report') as report:
        MainWindow._weekly_learning_report172(w)
    report.assert_called_once();assert w.thread_pool.errors==[]

def test_recovery_notice_distinguishes_preserved_backup_from_defaults(app):
    from jarvis.ui.main_window import MainWindow
    messages=[]
    config=SimpleNamespace(recovered_settings_backup='/test/damaged.json',repaired_settings=[],recovered_settings_source='/test/settings.last-good.json',default_settings_path='/test/default_settings.json')
    w=SimpleNamespace(config=config,_language_code=lambda:'ru',_append_assistant=lambda text,**kw:messages.append(text))
    MainWindow._announce_settings_recovery183(w)
    assert 'из сохранённой копии' in messages[-1] and 'по умолчанию' not in messages[-1]
    config.recovered_settings_source=config.default_settings_path
    MainWindow._announce_settings_recovery183(w)
    assert 'по умолчанию' in messages[-1]

def test_readiness_never_claims_disabled_vision_model_is_ready(app):
    from jarvis.ui.main_window import MainWindow
    messages=[]
    values={'ollama.chat_model':'qwen2.5:0.5b','ollama.vision_model':'vision:4b','local_chat.enabled':True,'ollama.vision_enabled':False,'ollama.preload_model':False}
    w=SimpleNamespace(config=SimpleNamespace(get=lambda k,d=None:values.get(k,d)),_set_status=messages.append,robot=SimpleNamespace(set_state=lambda x:None),_return_robot_to_idle=lambda:None)
    with patch('jarvis.ui.main_window.QTimer.singleShot'):
        MainWindow._ollama_checked(w,(True,'ok',['qwen2.5:0.5b']))
    assert 'qwen2.5:0.5b' in messages[-1] and 'vision:4b' not in messages[-1]
    values['local_chat.enabled']=False
    MainWindow._ollama_checked(w,(True,'ok',[]))
    assert 'отключены' in messages[-1] and 'готова' not in messages[-1]
