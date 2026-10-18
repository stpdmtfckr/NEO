"""Qt behavior for 146. Requires real PySide6; never substitutes mock Qt widgets.

Run with NEO.cmd --tests on Windows; offscreen checks are not Windows acceptance.
"""
from pathlib import Path
from types import SimpleNamespace
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
pytest.importorskip('PySide6', reason='Real Qt widget tests require PySide6')
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest
from jarvis.ui.bookshelf import Bookshelf, ObjectEditor
from jarvis.ui.chat_view import ChatView
from jarvis.ui.shelf_store import ShelfStore
from jarvis.ui.window_geometry import RoundedToolWindow, reachable_position

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])

class Config:
    def __init__(self, root):
        self.paths=SimpleNamespace(root=root)
        self.data={'app':{'language':'ru'},'shelf':{'restore_pinned':False,'background_color':'#123456','background_opacity_percent':85},'scenarios':{'items':[]}}
    def get(self,key,default=None):
        value=self.data
        for part in key.split('.'):
            if not isinstance(value,dict) or part not in value:return default
            value=value[part]
        return value
    def save(self,data): self.data=data


def flush(app):
    app.processEvents();QTest.qWait(30);app.processEvents()


def test_round_root_alpha_corner_and_opaque_center(app):
    window=RoundedToolWindow();window.resize(320,220);window.show();flush(app)
    image=window.grab().toImage()
    assert window.testAttribute(Qt.WA_TranslucentBackground)
    assert image.pixelColor(0,0).alpha()==0
    assert image.pixelColor(160,110).alpha()>240
    window.close();window.deleteLater();flush(app)


def test_checklist_checked_strikeout_undo_redo_and_reload(app,tmp_path):
    store=ShelfStore(tmp_path)
    item=store.create('checklist',items=[{'text':'Task','checked':False}])
    window=ObjectEditor(store,item,'ru',lambda:None)
    window.show();flush(app)
    window.checklist.item(0).setCheckState(Qt.Checked)
    assert window.checklist.item(0).font().strikeOut()
    window._undo_stack.undo()
    assert not window.checklist.item(0).font().strikeOut()
    window._undo_stack.redo()
    assert window.checklist.item(0).checkState()==Qt.Checked
    assert window.checklist.item(0).font().strikeOut()
    assert window.save()
    assert store.get(item['id'])['items'][0]['checked']
    window.close();window.deleteLater();flush(app)


def test_old_message_shrinks_after_font_change_and_keeps_author_timestamp(app):
    chat=ChatView();chat.resize(440,500);chat.set_user_name('Sas');chat.show()
    bubble=chat.add_user('Some wrapped text that needs multiple lines. '*14)
    chat.set_font_size(30);flush(app)
    before=bubble.height();stamp=bubble.item.timestamp;label=bubble.time_label.text()
    chat.set_font_size(10);flush(app)
    assert bubble.height()<before*.8
    assert bubble.height()<bubble.sizeHint().height()+40
    assert bubble.author_label.text()=='Sas'
    assert bubble.author_label.x()<bubble.time_label.x()
    chat.set_language('de');flush(app)
    assert bubble.author_label.text()=='Sas'
    assert bubble.item.timestamp==stamp and bubble.time_label.text()==label
    chat.close();chat.deleteLater();flush(app)


def test_shelf_background_has_independent_alpha(app,tmp_path):
    config=Config(tmp_path);shelf=Bookshelf(config,lambda _:None,lambda:None)
    shelf.show();flush(app)
    assert 'rgba(18,52,86,217)' in shelf.sections['notes'].styleSheet()
    config.data['shelf']['background_opacity_percent']=0
    shelf.apply_settings();flush(app)
    assert 'rgba(18,52,86,0)' in shelf.sections['notes'].styleSheet()
    assert shelf.windowOpacity()>.99
    shelf.shutdown();shelf.deleteLater();flush(app)


def test_reachability_preserves_negative_monitor_and_accessible_header(monkeypatch):
    import jarvis.ui.window_geometry as geometry
    area=QRect(-1920,-400,1920,1080)
    screen=SimpleNamespace(availableGeometry=lambda:area)
    monkeypatch.setattr(geometry,'QApplication',SimpleNamespace(screens=lambda:[screen],screenAt=lambda point:screen))
    widget=SimpleNamespace(parentWidget=lambda:None,width=lambda:420,height=lambda:300)
    point=reachable_position(widget,QPoint(-2200,-700))
    assert point==QPoint(-1920,-400)
    assert reachable_position(widget,QPoint(-1700,-200))==QPoint(-1700,-200)


def test_open_file_uses_exact_local_url_and_reports_failure(app,tmp_path,monkeypatch):
    import jarvis.ui.bookshelf as module
    config=Config(tmp_path);shelf=Bookshelf(config,lambda _:None,lambda:None)
    path=tmp_path/'literal name.txt';path.write_text('owned fixture')
    item=shelf.store.import_file(path,managed=False)
    seen=[];notices=[]
    monkeypatch.setattr(module.QDesktopServices,'openUrl',lambda url:seen.append(url.toLocalFile()) or True)
    monkeypatch.setattr(shelf,'_file_notice',notices.append)
    shelf.open_object(item['id'])
    assert len(seen)==1 and Path(seen[0]).resolve()==path.resolve() and not notices
    monkeypatch.setattr(module.QDesktopServices,'openUrl',lambda url:False)
    shelf.open_object(item['id']);assert notices
    shelf.shutdown();shelf.deleteLater();flush(app)


def test_recycle_cancel_or_failure_preserves_shelf_reference(app,tmp_path,monkeypatch):
    import jarvis.ui.qt_localization as localization
    import send2trash
    shelf=Bookshelf(Config(tmp_path),lambda _:None,lambda:None)
    path=tmp_path/'owned.txt';path.write_text('data')
    item=shelf.store.import_file(path,managed=False)
    view=SimpleNamespace(selected_objects=lambda:[item])
    notices=[];calls=[]
    monkeypatch.setattr(shelf,'_file_notice',notices.append)
    monkeypatch.setattr(send2trash,'send2trash',lambda value:calls.append(value))
    monkeypatch.setattr(localization,'localized_message_box',lambda *a,**kw:QMessageBox.Cancel)
    shelf.delete_selected_files(view)
    assert not calls and path.exists() and shelf.store.get(item['id'])
    monkeypatch.setattr(localization,'localized_message_box',lambda *a,**kw:QMessageBox.Yes)
    shelf.delete_selected_files(view)
    assert calls==[str(path)] and path.exists() and shelf.store.get(item['id']) and notices
    shelf.shutdown();shelf.deleteLater();flush(app)
