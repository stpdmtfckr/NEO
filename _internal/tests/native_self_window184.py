"""Native cloud Qt self-window QA. No real microphone/account/model/capture I/O."""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
os.environ['QT_QPA_PLATFORM'] = 'xcb'
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication
from pytest import MonkeyPatch
from tests.test_neo184_settings_mainwindow import window as isolated_window
from jarvis.runtime.self_window184 import queue_visibility

out = ROOT.parent.parent / 'evidence' / 'self_window' / 'native'
out.mkdir(parents=True, exist_ok=True)
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
monkeypatch = MonkeyPatch()
fixture = isolated_window.__wrapped__(app, out / 'profile', monkeypatch)
window = next(fixture)
window.setWindowTitle('NEO · isolated visibility QA')
window._rearm_voice_after_answer = lambda: None
window._ui_command_timer.start(50)
window.move(350, 90)
window.resize(480, 700)
window._append_assistant('Проверка окна: введите «спрячься». Через 2 секунды тест вернёт окно командой «покажись».', paced=False)
state = {'stage': 'waiting_for_typed_hide', 'checks': [], 'native_window_id': int(window.winId()),
         'windows_explorer_verified': False, 'platform': 'native Linux Qt/xcb'}
flags = window.windowFlags()
state['flags'] = {'minimize': bool(flags & Qt.WindowMinimizeButtonHint),
                  'system_menu': bool(flags & Qt.WindowSystemMenuHint),
                  'frameless': bool(flags & Qt.FramelessWindowHint)}
assert all(state['flags'].values())
window.grab().save(str(out / 'initial.png'))
started = time.monotonic()
changed = started
timer = QTimer(window)

def persist():
    (out / 'result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2))

def check():
    global changed
    now = time.monotonic()
    if state['stage'] == 'waiting_for_typed_hide' and getattr(window, '_runtime_demoted184', False):
        assert window.isVisible() and not window.isMinimized() and not window._quitting
        state['checks'].append('Actual typed command requested lowering; visible and not minimized, GUI timer active; native z-order unverified')
        state['stage'] = 'demoted'; changed = now; persist()
    elif state['stage'] == 'demoted' and now - changed > 2:
        window._append_assistant('Background result while demoted', paced=False)
        assert window.isVisible() and window._runtime_demoted184
        assert queue_visibility('show')
        state['stage'] = 'restoring'; persist()
    elif state['stage'] == 'restoring' and window.isVisible() and not window._runtime_demoted184:
        assert int(window.winId()) == state['native_window_id']
        assert not window._runtime_topmost and not window.testAttribute(Qt.WA_ShowWithoutActivating)
        state['checks'].append('Queued show restored same native window without sticky topmost/no-activate')
        window.showMinimized(); state['stage'] = 'minimized'; changed = now; persist()
    elif state['stage'] == 'minimized' and now - changed > 2:
        assert window.isMinimized()
        window._append_assistant('Background result while minimized', paced=False)
        assert window.isMinimized()
        window._show_from_tray()
        state['stage'] = 'tray_restored'; changed = now; persist()
    elif state['stage'] == 'tray_restored' and now - changed > 1:
        assert window.isVisible() and not window.isMinimized()
        for _ in range(3):
            window._set_runtime_topmost(False, hide=True)
            assert window.isVisible() and window._runtime_demoted184
            window._set_runtime_topmost(True)
            assert window.isVisible() and int(window.winId()) == state['native_window_id']
        state['checks'].append('Minimized stayed minimized under results; tray restore and repeated hide/show succeeded')
        state['stage'] = 'passed'; persist()
        window._append_assistant('Проверка пройдена: уход за окна, возврат, сворачивание и повторные команды работают.', paced=False)
        window.grab().save(str(out / 'passed.png'))
        print(json.dumps(state, ensure_ascii=False), flush=True)
    if now - started > 100:
        window.shutdown_resources()
        window.hide()
        app.quit()

persist(); timer.timeout.connect(check); timer.start(50)
try:
    app.exec()
finally:
    timer.stop()
    try:
        next(fixture)
    except StopIteration:
        pass
    monkeypatch.undo()
