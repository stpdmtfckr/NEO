"""Real MainWindow + settings signal integration under a disposable, disabled profile."""
from __future__ import annotations
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QObject, Signal, QTimer, QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication


def settle(app, duration=.04):
    deadline=time.monotonic()+duration
    while time.monotonic()<deadline:
        app.processEvents();time.sleep(.002)


@pytest.fixture(scope='module')
def app(): return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app,tmp_path,monkeypatch):
    for name in ('HOME','LOCALAPPDATA','APPDATA','XDG_CACHE_HOME'):
        monkeypatch.setenv(name,str(tmp_path/name))
    from jarvis.config import ConfigManager
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.settings_dialog import SettingsDialog
    # All actual runtime classes/UI are constructed. Hardware/background callbacks
    # are disabled in this isolated test, rather than faking the MainWindow itself.
    for name in ('_preload_stt_async','_warm_spoken_spelling','_ensure_wake_monitor','_resume_wake_listening',
                 '_pause_wake_listening','_register_app_mic_hotkey','_register_aux_hotkeys',
                 '_prewarm_discord_async','_ensure_voice_unlock_provider_async','_ensure_bookshelf',
                 '_show_activity_notice','_warm_up174','_poll_mail_async','_poll_bluetooth_devices_async',
                 '_startup_selfcheck173','_terminate_jarvis_helper_children'):
        monkeypatch.setattr(MainWindow,name,lambda *_a,**_kw:None)
    monkeypatch.setattr(SettingsDialog,'_refresh_microphones_async',lambda *_:None)
    monkeypatch.setattr(SettingsDialog,'_start_game_cache_stats158',lambda *_:None)
    monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *_:None)
    monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *_:None)
    with external_io_blocked(),patch.object(socket.socket,'sendto',side_effect=PermissionError('QA datagram block')),patch.object(os,'system',side_effect=PermissionError('QA shell block')):
        c=ConfigManager(ROOT/'config/default_settings.json')
        for group in ('voice','wake_word','briefing','startup_self_test','voice_unlock','activity','file_index',
                      'ambient_learning','gaming','downloads','neo_model','llm_agent','plugins'):
            c.data.setdefault(group,{})['enabled']=False
        c.data['ollama'].update(vision_enabled=False,auto_start=False,auto_install=False,auto_pull_models=False,requests_enabled=False)
        c.data['llm_agent'].setdefault('finetune',{})['auto']=False
        c.data['neo_model'].setdefault('schedule',{})['enabled']=False
        c.data['briefing']['show_on_startup']=False
        c.data['ui'].update(start_with_windows=False,priority_startup=False,robot_only_mode=False,animation_fps=30)
        c.data['app']['language']='ru'
        c.data['history']['enabled']=False
        for values in c.data.get('integrations',{}).values():
            if isinstance(values,dict): values['enabled']=False
        c.data['integrations']['telegram'].update(remote_control_enabled=False,remote_wake_enabled=False,messages_enabled=False)
        # Startup-only singleShot callbacks include app-lifetime hardware jobs.
        # Suppress them during construction; real settings timers remain active.
        with patch.object(QTimer, "singleShot", lambda *_a, **_k: None):
            w=MainWindow(c)
        for timer in w.findChildren(QTimer): timer.stop()
        w.resize(900,720);w.show();settle(app,.1)
        yield w
        dialog=getattr(w,'_settings_dialog',None)
        if dialog is not None: dialog.reject()
        w.shutdown_resources();w.hide();w.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);settle(app)


class FakeOperation(QObject):
    progress=Signal(str,int)
    completed=Signal(dict)
    failed=Signal(str)
    finished=Signal()
    def __init__(self,action,model,base_url,**kw):
        super().__init__(QApplication.instance());self.action=action;self.model=model;self.base_url=base_url;self.cancelled=False;self.started=False
        self.finished.connect(self.deleteLater, Qt.QueuedConnection)
    def start(self): self.started=True
    def cancel(self): self.cancelled=True


def open_settings(window,app):
    window._open_settings()
    deadline=time.monotonic()+5
    d=window._settings_dialog
    while not d._settings_ready160 and not d._settings_build_error160 and time.monotonic()<deadline:
        settle(app,.015)
    assert d._settings_ready160
    d.tabs.setCurrentIndex(8)
    return d


def test_real_window_model_progress_and_controls_keep_scroll(window,app,monkeypatch):
    import jarvis.model_management as service
    from jarvis.ui.settings_dialog import QMessageBox
    monkeypatch.setattr(service,'ModelOperation',FakeOperation)
    choices=[]
    monkeypatch.setattr(QMessageBox,'question',lambda *a:(choices.append(a),QMessageBox.Yes)[1])
    for index in range(35):
        window.chat.add_assistant('История для проверки '+str(index)+' '+('строка '*28))
    # Wait for successive queued Qt layout passes, rather than assuming a fixed
    # 120 ms drain already published the scroll range on a busy test host.
    bar=window.chat.verticalScrollBar()
    deadline=time.monotonic()+2.0
    while bar.maximum()<=300 and time.monotonic()<deadline:
        app.processEvents();time.sleep(.002)
    assert bar.maximum()>300
    bar.setValue(bar.maximum()//3);settle(app)
    expected=bar.value()
    d=open_settings(window,app)
    d.chat_model.setCurrentText('test-only:0.5b')
    for action,button in (('check',d.chat_model_check184),('download',d.chat_model_download184),('delete',d.chat_model_delete184)):
        button.click();operation=d._model_operation184
        assert operation is not None and operation.action==action and operation.started
        operation.progress.emit('Проверка тестовой модели '+action,50);settle(app)
        bubble=window._model_progress_bubble
        assert bubble is not None and action in bubble.body_label.text()
        assert bar.value()==expected
        if action=='download':
            d.chat_model_cancel184.click();assert operation.cancelled
            operation.completed.emit({'status':'cancelled','message':'Тест: отменено'})
        else:operation.completed.emit({'status':'installed' if action=='check' else 'deleted','message':'Тест: '+action,'models':['test-only:0.5b']})
        operation.finished.emit();settle(app)
        assert d._model_operation184 is None
        assert bar.value()==expected
        assert d.chat_model_download184.isEnabled()
    assert 'test-only:0.5b' in choices[-1][2]
    d.reject();settle(app)


def test_real_window_repeated_close_cancels_and_relays_late_result(window,app,monkeypatch):
    import jarvis.model_management as service
    from jarvis.ui.settings_dialog import SettingsDialog
    monkeypatch.setattr(service,'ModelOperation',FakeOperation)
    baseline=len(window.findChildren(SettingsDialog))
    live_counts=[]
    for index in range(3):
        d=open_settings(window,app)
        d.chat_model_download184.click();operation=d._model_operation184
        operation.progress.emit('Начат тест '+str(index),20);settle(app)
        bubble=window._model_progress_bubble
        before=bubble.body_label.text()
        d.reject();assert operation.cancelled
        assert bubble.body_label.text()=='Отменяю операцию…'
        assert bubble.body_label.text()!=before
        operation.progress.emit('НЕ ДОЛЖНО ПОЯВИТЬСЯ',99)
        operation.completed.emit({'status':'cancelled','message':'ПОЗДНИЙ РЕЗУЛЬТАТ'})
        operation.finished.emit();settle(app)
        assert window._model_progress_bubble is bubble
        assert bubble.body_label.text()=='ПОЗДНИЙ РЕЗУЛЬТАТ'
        assert window._settings_dialog is None
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);settle(app)
        live_counts.append(len(window.findChildren(SettingsDialog)))
    assert live_counts[-1]<=baseline+1, f'Closed SettingsDialog children retained: {live_counts}'


def test_real_window_constructor_and_shutdown_smoke(window):
    assert window._ui_ready and window.chat is not None
