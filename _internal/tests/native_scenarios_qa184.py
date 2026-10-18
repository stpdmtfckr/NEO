"""Disposable native Qt scenario QA; no real launch, provider, or account actions."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
source = Path(__file__).resolve().parents[1]
profile = source.parents[1] / 'evidence' / 'scenarios' / 'native_profile'
profile.mkdir(parents=True, exist_ok=True)
os.environ.update(HOME=str(profile), LOCALAPPDATA=str(profile/'local'), APPDATA=str(profile/'roam'),
                  NEO_TEST_MODE='1', PYTHONDONTWRITEBYTECODE='1', QT_QPA_PLATFORM='xcb')
sys.dont_write_bytecode=True
sys.path.insert(0,str(source))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from jarvis.config import ConfigManager
from jarvis.ui.settings_dialog import SettingsDialog
from jarvis.testing.isolation import external_io_blocked
app=QApplication([])
with external_io_blocked(), patch.object(SettingsDialog,'_refresh_microphones_async',lambda *_:None), patch.object(SettingsDialog,'_start_game_cache_stats158',lambda *_:None), patch.object(ConfigManager,'apply_autostart',lambda *_:None), patch.object(ConfigManager,'apply_wake_service',lambda *_:None):
    config=ConfigManager(source/'config/default_settings.json')
    config.data['app']['language']='ru'
    config.data['scenarios']['items']=[{'id':'qa-work','name':'Рабочий режим · QA','phrase':'начать работу','aliases':['офис'],'on_failure':'stop','steps':[{'type':'wait','seconds':0.5},{'type':'wait','seconds':2}]}]
    config.save(config.data)
    dialog=SettingsDialog(config)
    dialog.setWindowTitle('NEO scenario editor · isolated QA')
    dialog.scenario_run_requested.connect(lambda sid: print('CONFIRMED ROUTE REQUEST (FAKE ONLY): '+sid,flush=True))
    dialog.show()
    def select_scenarios():
        for index, entry in enumerate(dialog._settings_pages160):
            if entry[1]=='Сценарии': dialog.tabs.setCurrentIndex(index);break
    QTimer.singleShot(150, select_scenarios)
    QTimer.singleShot(600000, dialog.reject)
    result=app.exec()
    print('FINAL_SCENARIOS='+json.dumps(config.get('scenarios.items'),ensure_ascii=False),flush=True)
    sys.exit(result)
