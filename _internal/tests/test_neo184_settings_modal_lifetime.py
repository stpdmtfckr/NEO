from types import SimpleNamespace
from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication,QWidget,QDialog,QSpinBox,QMessageBox
from PySide6.QtCore import QCoreApplication,QEvent
from jarvis.ui.settings_dialog import SettingsDialog

@pytest.fixture
def host(tmp_path):
    app=QApplication.instance() or QApplication([])
    w=QWidget();w.config=SimpleNamespace(paths=SimpleNamespace(root=tmp_path));w._robot_gif_states={};w._robot_gif_scale=1
    w._settings_ready160=True;w._settings_closed160=False;w._t=lambda x:x;w._rt=lambda x:x
    yield app,w
    w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);app.processEvents()

def flush(app):
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);app.processEvents()

@pytest.mark.parametrize('accepted',[False,True])
def test_gif_modal_is_disposed_after_each_close(host,monkeypatch,accepted):
    app,w=host
    import jarvis.ui.gif_settings as gif
    class FakeGif(QDialog):
        def __init__(self,*args):
            super().__init__(args[-1]);self.mapping={'idle':'test.gif'};self.scale=QSpinBox(self);self.scale.setValue(42)
        def exec(self):return QDialog.Accepted if accepted else QDialog.Rejected
    monkeypatch.setattr(gif,'GifSettingsDialog',FakeGif)
    for _ in range(5):
        SettingsDialog._open_gif_settings(w);flush(app)
        assert not w.findChildren(FakeGif)
    assert w._robot_gif_states==({'idle':'test.gif'} if accepted else {})
    assert w._robot_gif_scale==(42 if accepted else 1)

def test_cancelled_reset_modal_is_disposed(host,monkeypatch):
    app,w=host;monkeypatch.setattr(QMessageBox,'exec',lambda _:QMessageBox.Cancel)
    for _ in range(5):
        SettingsDialog._reset_all_settings(w);flush(app)
        assert not w.findChildren(QMessageBox)

@pytest.mark.parametrize('keep',[False,True])
def test_reset_choice_is_read_before_disposal(host,monkeypatch,keep):
    app,w=host;calls=[]
    w.config.reset_to_defaults=lambda **kw:calls.append(kw)
    w.config.stored_credentials=lambda:[]
    w.settings_saved=SimpleNamespace(emit=lambda:calls.append('saved'))
    w.accept=lambda:calls.append('accepted')
    role=QMessageBox.AcceptRole if keep else QMessageBox.YesRole
    monkeypatch.setattr(QMessageBox,'exec',lambda _:QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox,'clickedButton',lambda box:next(b for b in box.buttons() if box.buttonRole(b)==role))
    monkeypatch.setattr(QMessageBox,'information',lambda *a:None)
    SettingsDialog._reset_all_settings(w);flush(app)
    assert calls==[{'keep_connections':keep},'saved','accepted']
    assert not w.findChildren(QMessageBox)

def test_reset_modal_is_disposed_when_exec_raises(host,monkeypatch):
    app,w=host
    def fail(_):raise RuntimeError('synthetic dialog failure')
    monkeypatch.setattr(QMessageBox,'exec',fail)
    with pytest.raises(RuntimeError,match='synthetic dialog failure'):
        SettingsDialog._reset_all_settings(w)
    flush(app)
    assert not w.findChildren(QMessageBox)
