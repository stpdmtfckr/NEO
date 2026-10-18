from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from jarvis.runtime import scenarios as s
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.testing.isolation import external_io_blocked

class Config:
    def __init__(self, steps):
        self.paths = SimpleNamespace(root=Path('/disposable/retry-review'))
        self.item = {'id':'r', 'name':'Review', 'enabled':True, 'max_seconds':1, 'on_failure':'continue', 'steps':steps}
    def get(self, key, default=None):
        return [self.item] if key == 'scenarios.items' else default

@pytest.fixture(autouse=True)
def isolated():
    cancellation_manager().reset()
    s._RUN_RECORDS.clear()
    s._HISTORY.clear()
    with external_io_blocked():
        yield
    cancellation_manager().reset()
    s._RUN_RECORDS.clear()
    s._HISTORY.clear()

def test_timeout_between_steps_does_not_make_unstarted_command_retryable():
    config = Config([{'type':'open','path':'inert.exe'}, {'type':'command','command':['never-launch']}])
    clock = [0.0]
    def on_event(event):
        if event['state'] == 'step_finished' and event['step_index'] == 1:
            clock[0] = 2.0
    with patch.object(s.time, 'monotonic', lambda: clock[0]), patch.object(s, '_open_path', lambda _: {'success':False,'error':'inert open failed'}):
        result = s.run_scenario(config, 'r', on_event=on_event)
    assert result.outcomes[1]['not_run'] is True
    with pytest.raises(ValueError):
        s.scenario_retry_request(config, result.run_id, 2)

def test_cancel_between_steps_does_not_claim_unstarted_command_opened():
    config = Config([{'type':'open','path':'inert.exe'}, {'type':'command','command':['never-launch']}])
    def on_event(event):
        if event['state'] == 'step_finished' and event['step_index'] == 1:
            s.cancel_scenario(event['run_id'])
    with patch.object(s, '_open_path', lambda _: {'success':True}):
        result = s.run_scenario(config, 'r', on_event=on_event)
    assert result.outcomes[1]['not_run'] is True
    assert not result.outcomes[1].get('opened')

def test_boundary_timeout_retry_never_runs_unstarted_command():
    config = Config([{'type':'open','path':'inert.exe'}, {'type':'command','command':['never-launch']}])
    clock, commands = [0.0], []
    def on_event(event):
        if event['state'] == 'step_finished' and event['step_index'] == 1:
            clock[0] = 2.0
    with patch.object(s.time, 'monotonic', lambda: clock[0]), patch.object(s, '_open_path', lambda _: {'success':False,'error':'inert open failed'}):
        result = s.run_scenario(config, 'r', on_event=on_event)
        try:
            args = s.scenario_retry_request(config, result.run_id, 2)
        except ValueError:
            return
        clock[0] = 0.0
        with patch.object(s, '_command', lambda *args: commands.append('command') or {'success':True}):
            retried = s.retry_scenario(config, args)
    assert commands == []

@pytest.fixture(scope='session')
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])

@pytest.mark.parametrize('bad_field', ['nan_budget', 'missing_steps'])
def test_preview_handles_editable_invalid_definition(app, bad_field):
    from PySide6.QtCore import QCoreApplication, QEvent
    from jarvis.ui.scenario_confirmation184 import ScenarioConfirmationDialog
    raw = {'id':'p','name':'Draft','enabled':False,'steps':[{'type':'wait','seconds':0}]}
    if bad_field == 'nan_budget':
        raw['max_seconds'] = float('nan')
    else:
        raw['steps'] = None
    dialog = ScenarioConfirmationDialog(None, raw, 'en', review_only=True)
    assert dialog.preview.toPlainText()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

@pytest.mark.parametrize('language', ['ru','en','de','fr','es','it','pt'])
def test_populated_retry_controls_fit_narrow_editor(app, language):
    from PySide6.QtCore import QCoreApplication, QEvent
    from jarvis.ui.scenario_editor import ScenarioEditor
    from tests.test_scenario_workflows184 import Config as UIConfig, item
    config = UIConfig([item([{'type':'open','path':'inert.exe','monitor':1,'launch_wait':0}])], language=language)
    with patch.object(s, '_open_path', lambda _: {'success':True}), patch.object(s, '_move_opened_window', lambda *args: {'moved':False,'error':'window not found'}):
        s.run_scenario(config, 'test-id')
    editor = ScenarioEditor(config)
    editor.resize(450,850)
    editor.show()
    app.processEvents()
    width = editor.width()
    editor.close()
    editor.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert width <= 470

def test_agent_pending_retry_survives_new_shorter_run(app):
    from jarvis.ui.main_window import MainWindow
    from tests.test_scenario_workflows184 import Config as UIConfig, item
    cfg = UIConfig([item([{'type':'wait','seconds':0}, {'type':'open','path':'inert.exe'}])])
    with patch.object(s, '_open_path', lambda _: {'success':False,'error':'missing','effect_state':'none'}):
        old = s.run_scenario(cfg, 'test-id')
    args = s.scenario_retry_request(cfg, old.run_id, 2)
    cfg.data['scenarios']['items'][0]['steps'] = [{'type':'wait','seconds':0}]
    s.run_scenario(cfg, 'test-id')
    h = SimpleNamespace(config=cfg,_flush_response_stream=lambda:None,_stream_flush_timer=SimpleNamespace(stop=lambda:None),_append_tool_details=lambda _:None,_localize_text=lambda x:x,_pending_response_bubble=None,chat=SimpleNamespace(update_bubble=lambda *_:None),_set_busy=lambda *_:None,_agent_generation=1,_language_code=lambda:'en')
    turn = SimpleNamespace(text='Confirm',tool_details=[],pending=SimpleNamespace(tool_name='scenario',arguments=args,summary='raw'))
    MainWindow._handle_agent_turn(h,turn)
