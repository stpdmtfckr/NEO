"""NB19: real Qt windows, disposable profiles, inert model operations only.

Run with the recovered mutable _internal tree on PYTHONPATH. The imported
window fixture blocks external I/O and disables hardware/background callbacks.
"""
from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from tests.test_neo184_settings_mainwindow import app, window, settle, open_settings, FakeOperation


@pytest.fixture
def operations(monkeypatch):
    import jarvis.model_management as service
    from jarvis.ui.settings_dialog import QMessageBox
    monkeypatch.setattr(service, 'ModelOperation', FakeOperation)
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.Yes)


def destroy_dialog(dialog, app):
    dialog.reject()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    settle(app)
    assert not isValid(dialog), 'Terminal delivery must work after actual QObject destruction'


def start(dialog, action, app):
    dialog._start_model_operation184(action)
    operation = dialog._model_operation184
    relay = dialog._model_relay184
    assert operation.started
    operation.progress.emit('test-only:0.5b: pulling manifest (42%)', 42)
    settle(app)
    return operation, relay


def finish(operation, status, message, app):
    operation.completed.emit({'status': status, 'message': message})
    operation.finished.emit()
    settle(app)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    settle(app)


def test_closed_dialog_terminal_replaces_progress_and_releases_relay(window, app, operations):
    from jarvis.ui.settings_dialog import _ModelOperationRelay184
    count = len(app.findChildren(_ModelOperationRelay184))
    d = open_settings(window, app)
    operation, relay = start(d, 'download', app)
    bubble = window._model_progress_bubble
    assert '42%' in bubble.body_label.text()
    destroy_dialog(d, app)
    assert operation.cancelled
    assert bubble.body_label.text() == 'Отменяю операцию…'
    assert isValid(relay)
    operation.progress.emit('STALE PROGRESS 99%', 99)
    settle(app)
    assert bubble.body_label.text() == 'Отменяю операцию…'
    finish(operation, 'cancelled', 'Операция с моделью отменена.', app)
    assert bubble.body_label.text() == 'Операция с моделью отменена.'
    assert not isValid(relay)
    assert len(app.findChildren(_ModelOperationRelay184)) == count
    assert window._model_progress_bubbles184 == {}
    assert window._settings_dialog is None


def test_three_closed_reopened_operations_finish_out_of_order_without_scroll(window, app, operations):
    from jarvis.ui.settings_dialog import SettingsDialog, _ModelOperationRelay184
    for index in range(30):
        window.chat.add_assistant('History ' + str(index) + ': ' + 'keep reading here ' * 30)
    settle(app, .1)
    bar = window.chat.verticalScrollBar()
    bar.setValue(bar.maximum() // 3)
    position = bar.value()
    baseline = len(window.findChildren(SettingsDialog))
    relay_count = len(app.findChildren(_ModelOperationRelay184))
    pending = []
    for index in range(3):
        d = open_settings(window, app)
        operation, relay = start(d, 'download', app)
        bubble = window._model_progress_bubble
        pending.append((operation, relay, bubble))
        d.chat_model_cancel184.click()
        d.chat_model_cancel184.click()
        destroy_dialog(d, app)
        assert operation.cancelled and bubble.body_label.text() == 'Отменяю операцию…'
        assert bar.value() == position
    latest = pending[-1][2]
    assert len({id(row[2]) for row in pending}) == 3
    for index in (2, 0, 1):
        operation, relay, bubble = pending[index]
        finish(operation, 'cancelled', 'Cancelled operation ' + str(index), app)
        assert bubble.body_label.text() == 'Cancelled operation ' + str(index)
        assert window._model_progress_bubble is latest
        if index != 2:
            assert latest.body_label.text() == 'Cancelled operation 2'
        assert not isValid(relay)
        assert bar.value() == position
    assert len(window.findChildren(SettingsDialog)) == baseline
    assert len(app.findChildren(_ModelOperationRelay184)) == relay_count
    assert window._model_progress_bubbles184 == {}


@pytest.mark.parametrize('language', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_cancel_request_then_committed_delete_and_uncertain_error_localized(window, app, operations, language):
    from jarvis.i18n import tr
    from jarvis.ui.settings_text184 import model_text
    window.config.data['app']['language'] = language
    for outcome in ('deleted', 'failed', 'cancelled'):
        d = open_settings(window, app)
        operation, relay = start(d, 'delete' if outcome != 'cancelled' else 'download', app)
        bubble = window._model_progress_bubble
        destroy_dialog(d, app)
        assert bubble.body_label.text() == tr('Отменяю операцию…', language)
        if outcome == 'deleted':
            message = 'Модель test-only:0.5b удалена.'
            operation.completed.emit({'status': 'deleted', 'message': message, 'models': []})
        elif outcome == 'failed':
            message = 'Локальная служба Ollama не отвечает. Запустите Ollama и повторите проверку; если она не установлена: https://ollama.com/download. Результат операции не подтверждён.'
            operation.failed.emit(message)
        else:
            message = 'Операция с моделью отменена.'
            operation.completed.emit({'status': 'cancelled', 'message': message})
        operation.finished.emit()
        settle(app)
        assert bubble.item.text == model_text(message, language)
        # Chat inserts zero-width URL wrap opportunities in the rendered label.
        assert bubble.body_label.text().replace('\u200b', '') == model_text(message, language)
        assert window._model_progress_bubbles184 == {}


def test_terminal_wins_over_queued_progress_duplicate_results_and_close(window, app, operations):
    d = open_settings(window, app)
    operation, relay = start(d, 'check', app)
    bubble = window._model_progress_bubble
    operation.completed.emit({'status': 'installed', 'message': 'Verified installed'})
    operation.progress.emit('STALE PROGRESS', 99)
    operation.failed.emit('STALE ERROR')
    operation.completed.emit({'status': 'cancelled', 'message': 'STALE CANCEL'})
    settle(app)
    assert bubble.body_label.text() == 'Verified installed'
    assert d.chat_model_status184.text() == 'Verified installed'
    destroy_dialog(d, app)
    assert not operation.cancelled
    assert bubble.body_label.text() == 'Verified installed'
    operation.finished.emit()
    operation.finished.emit()
    settle(app)
    assert window._model_progress_bubbles184 == {}


def test_finished_without_result_reports_uncertainty(window, app, operations):
    from jarvis.ui.settings_dialog import _ModelOperationRelay184
    d = open_settings(window, app)
    operation, relay = start(d, 'delete', app)
    bubble = window._model_progress_bubble
    destroy_dialog(d, app)
    operation.finished.emit()
    settle(app)
    assert bubble.body_label.text() == _ModelOperationRelay184._UNCONFIRMED['ru']
    assert window._model_progress_bubbles184 == {}


def test_worker_thread_completion_uses_gui_receiver(window, app, operations, monkeypatch):
    d = open_settings(window, app)
    operation, relay = start(d, 'download', app)
    bubble = window._model_progress_bubble
    destroy_dialog(d, app)
    original = window.chat.update_bubble
    threads = []
    def update(target, text):
        threads.append(QThread.currentThread())
        return original(target, text)
    monkeypatch.setattr(window.chat, 'update_bubble', update)
    worker = threading.Thread(target=lambda: (
        operation.completed.emit({'status': 'cancelled', 'message': 'Worker result'}),
        operation.finished.emit()))
    worker.start()
    worker.join()
    settle(app)
    assert bubble.body_label.text() == 'Worker result'
    assert threads and all(thread == app.thread() for thread in threads)


def test_reset_conversation_discards_old_results_without_reviving_bubble(window, app, operations):
    d = open_settings(window, app)
    operation, relay = start(d, 'download', app)
    destroy_dialog(d, app)
    window._reset_conversation()
    settle(app)
    before = [id(item) for item in window.chat.items]
    finish(operation, 'cancelled', 'OLD RESULT', app)
    # The new-conversation notice may still be typing. No old result may
    # create a row, replace its text, or become the current progress bubble.
    assert [id(item) for item in window.chat.items] == before
    assert all('OLD RESULT' not in item.text for item in window.chat.items)
    assert window._model_progress_bubble is None
    assert window._model_progress_bubbles184 == {}


def test_validation_notice_does_not_overwrite_pending_operation(window, app, operations):
    d = open_settings(window, app)
    operation, relay = start(d, 'download', app)
    old_bubble = window._model_progress_bubble
    destroy_dialog(d, app)
    newer = open_settings(window, app)
    newer.ollama_url.setText('https://example.invalid')
    newer._start_model_operation184('check')
    notice = window._model_progress_bubble
    assert notice is not old_bubble
    assert newer._model_operation184 is None
    before = notice.body_label.text()
    finish(operation, 'cancelled', 'Old cancellation confirmed', app)
    assert old_bubble.body_label.text() == 'Old cancellation confirmed'
    assert notice.body_label.text() == before
    assert window._model_progress_bubble is notice


def test_closed_delete_terminal_invalidates_only_its_captured_server(window,app,operations):
    import time
    from jarvis.runtime import local_chat169
    c=window.agent.ollama
    d=open_settings(window,app)
    operation,relay=start(d,'delete',app)
    original=c.base_url
    c._model_cache=(time.monotonic(),['installed-before-delete'])
    generation=c._model_cache_generation
    destroy_dialog(d,app)
    assert c._model_cache_generation==generation
    finish(operation,'deleted','Модель test-only:0.5b удалена.',app)
    assert c._model_cache==(0.0,[]) and c._model_cache_generation>generation
    # An older server's completed operation must not affect a new server's state.
    d=open_settings(window,app);operation,relay=start(d,'download',app)
    destroy_dialog(d,app)
    window.config.data['ollama']['base_url']='http://127.0.0.1:22434'
    c.refresh_model_settings()
    c._model_cache=(time.monotonic(),['new-server-model'])
    local_chat169._UNAVAILABLE_UNTIL[0]=time.monotonic()+60
    until=local_chat169._UNAVAILABLE_UNTIL[0];generation=c._model_cache_generation
    finish(operation,'installed','Модель test-only:0.5b скачана и проверена.',app)
    assert c._model_cache_generation==generation and c._model_cache[1]==['new-server-model']
    assert local_chat169._UNAVAILABLE_UNTIL[0]==until
    window.config.data['ollama']['base_url']=original
    local_chat169.reset_availability()


def test_out_of_order_mutation_terminals_force_fresh_inventory(window,app,operations):
    c=window.agent.ollama
    d=open_settings(window,app);older,old_relay=start(d,'delete',app);destroy_dialog(d,app)
    d=open_settings(window,app);newer,new_relay=start(d,'download',app);destroy_dialog(d,app)
    newer.completed.emit({'status':'installed','ready':True,'message':'Модель new:1b скачана и проверена.','models':['new:1b']})
    newer.finished.emit();settle(app)
    assert c._model_cache[1]==['new:1b']
    older.completed.emit({'status':'deleted','ready':False,'message':'Модель old:1b удалена.','models':['old-stale:1b']})
    older.finished.emit();settle(app)
    assert c._model_cache==(0.0,[])
    assert not window._model_inventory_operations184
