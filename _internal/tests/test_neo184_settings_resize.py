"""Geometry changes stay live without whole-dialog theme repolishing."""
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea
from jarvis.ui.settings_dialog import SettingsDialog
from jarvis.testing.isolation import external_io_blocked
from tests.test_neo158_settings_io import app,config

def test_drag_sequence_keeps_live_layout_without_theme_storm(app,config,monkeypatch):
 with external_io_blocked():
  d=SettingsDialog(config);d.show();QTest.qWait(50)
  calls=[];original=d.setStyleSheet
  monkeypatch.setattr(d,'setStyleSheet',lambda text:calls.append(text) or original(text))
  scale=d._ui_scale
  for width,height in [(1000,660),(950,640),(800,550),(600,420),(700,490),(1100,740)]:
   d.resize(width,height);QTest.qWait(35)
   assert (d.width(),d.height())==(width,height)
   assert d.tabs._nav.width()<=236
  assert calls==[] and d._ui_scale==scale
  assert not d._theme_resize_timer.isActive()
  d.reject();d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

def test_palette_changes_still_repaint_after_resize(app,config,monkeypatch):
 with external_io_blocked():
  d=SettingsDialog(config);d.show();QTest.qWait(30);d.resize(600,420);QTest.qWait(35)
  calls=[];original=d.setStyleSheet
  monkeypatch.setattr(d,'setStyleSheet',lambda text:calls.append(text) or original(text))
  config.data['ui']['accent']='#FEC321';d._refresh_dialog_theme();QTest.qWait(10)
  assert len(calls)==1 and '#fec321' in calls[0]
  d._refresh_dialog_theme();assert len(calls)==1
  d.reject();d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

def test_new_scenario_and_dynamic_developer_page_fit_after_resize(app,config):
 with external_io_blocked():
  d=SettingsDialog(config);d.show();d.resize(600,420);QTest.qWait(40)
  for index in (10,12):
   d.tabs.setCurrentIndex(index)
   if index==12:d.developer_settings_enabled.setChecked(True)
   QTest.qWait(35);page=d.tabs._stack.currentWidget()
   assert isinstance(page,QScrollArea)
   assert page.horizontalScrollBar().maximum()==0
  d.reject();d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
