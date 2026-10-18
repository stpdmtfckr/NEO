"""Voice layout and live-language checks; no model, network, audio or process IO."""
from __future__ import annotations
from pathlib import Path
from unittest.mock import Mock
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea
from jarvis.testing.isolation import external_io_blocked
from jarvis.ui.response_voice_page import ResponseVoicePage
from jarvis.response_voice.text import text, CODES
from tests.test_response_voice_qwen import Config


@pytest.fixture(autouse=True)
def inert():
    with external_io_blocked():yield


@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])


class IdleService:
    def snapshot(self):return {'state':'unchecked','voice_state':'idle','done':0,'total':2498386873,'running':False,'active':False,'playing':False,'runtime_installed':False}
    def cancel(self):pass
    def cancel_operation(self):pass
    def close(self):pass
    def model_operation(self,action):return False
    def submit(self,*args,**kwargs):return False


@pytest.mark.parametrize('language',CODES)
@pytest.mark.parametrize('width',[400,500,600])
def test_voice_actions_reflow_without_horizontal_scroll(app,tmp_path,language,width):
    config=Config(tmp_path);config.data['app']['language']=language
    page=ResponseVoicePage(config,service=IdleService())
    area=QScrollArea();area.setWidgetResizable(True);area.setWidget(page);area.resize(width,420);area.show()
    try:
        app.processEvents();QTest.qWait(10)
        assert area.horizontalScrollBar().maximum()==0
        for control in [page.speaker,*page.fields.values(),*page.buttons.values()]:
            area.ensureWidgetVisible(control);app.processEvents()
            point=control.mapTo(area.viewport(),QPoint(0,0))
            assert point.x()>=0
            assert point.x()+control.width()<=area.viewport().width()+1
        # Exercise both directions; a formerly-wide page must release its minimum width.
        area.resize(800,600);app.processEvents();QTest.qWait(10)
        assert page._action_columns==2
        area.resize(width,420);app.processEvents();QTest.qWait(10)
        assert area.horizontalScrollBar().maximum()==0
    finally:
        page.close_operations();area.close();area.deleteLater();app.processEvents()


@pytest.mark.parametrize('language',['ru','de','fr'])
def test_keyboard_reaches_all_enabled_controls_after_narrow_reflow(app,tmp_path,language):
    config=Config(tmp_path);config.data['app']['language']=language
    page=ResponseVoicePage(config,service=IdleService())
    area=QScrollArea();area.setWidgetResizable(True);area.setWidget(page);area.resize(400,420);area.show()
    try:
        app.processEvents();page.enabled.setFocus();app.processEvents()
        targets=[page.enabled,page.speaker,*page.fields.values(),*[button for button in page.buttons.values() if button.isEnabled()]]
        reached=set()
        for _ in range(50):
            current=app.focusWidget()
            for control in targets:
                if current is control or (current is not None and control.isAncestorOf(current)):reached.add(id(control))
            QTest.keyClick(current or page,Qt.Key_Tab);app.processEvents()
        assert reached=={id(control) for control in targets}
        assert area.horizontalScrollBar().maximum()==0
    finally:
        page.close_operations();area.close();area.deleteLater();app.processEvents()


def test_live_page_language_refresh_preserves_preferences_and_updates_every_caption(app,tmp_path):
    config=Config(tmp_path);page=ResponseVoicePage(config,service=IdleService())
    area=QScrollArea();area.setWidgetResizable(True);area.setWidget(page);area.resize(400,420);area.show()
    try:
        page.speaker.setCurrentIndex(1);page.fields['volume'].setValue(37);before=page.values()
        for code in ('de','ru','fr','en'):
            config.data['app']['language']=code
            QTest.qWait(240)
            assert page.language==code and page.enabled.text()==text('enabled',code)
            assert page.speaker.itemText(1)==text('female',code)
            assert page.help_label.text()==text('help',code)
            assert page.status.text()==text('unchecked',code)
            assert page._labels['idle'].text()==text('idle',code)
            assert page.fields['volume'].toolTip()==text('volume',code)
            assert page.fields['volume'].accessibleName()==text('volume',code)
            assert all(button.text()==text(key,code) for key,button in page.buttons.items())
            assert page.values()==before and area.horizontalScrollBar().maximum()==0
    finally:
        page.close_operations();area.close();area.deleteLater();app.processEvents()


def test_real_settings_apply_updates_voice_language_at_600_width(app,tmp_path,monkeypatch):
    from jarvis.config import ConfigManager
    from jarvis.ui.settings_dialog import SettingsDialog,QMessageBox
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'));monkeypatch.setenv('APPDATA',str(tmp_path/'roaming'))
    monkeypatch.setattr(SettingsDialog,'_refresh_microphones_async',lambda *_:None)
    monkeypatch.setattr(SettingsDialog,'_start_game_cache_stats158',lambda *_:None)
    monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *_:None)
    monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *_:None)
    errors=[];monkeypatch.setattr(QMessageBox,'critical',lambda *a:errors.append(a))
    config=ConfigManager(Path(__file__).resolve().parents[1]/'config/default_settings.json')
    config.data['app']['language']='en'
    dialog=SettingsDialog(config);dialog.resize(600,420);dialog.tabs.setCurrentIndex(len(dialog._settings_pages160)-1);dialog.show()
    try:
        page=dialog.response_voice_page;app.processEvents()
        dialog.app_language.setCurrentIndex(dialog.app_language.findData('de'))
        dialog._save(close=False);QTest.qWait(260)
        assert not errors and page.language=='de'
        assert page.enabled.text()==text('enabled','de') and page.help_label.text()==text('help','de')
        assert page.speaker.itemText(0)==text('male','de')
        area=page.parentWidget().parentWidget()
        assert isinstance(area,QScrollArea) and area.horizontalScrollBar().maximum()==0
    finally:
        dialog.reject();dialog.deleteLater();app.processEvents()
