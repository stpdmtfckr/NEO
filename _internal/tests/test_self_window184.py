"""Self-window routing and GUI lifecycle; all audio/account/LLM I/O is inert."""
from __future__ import annotations

import copy
import ctypes
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis.runtime.self_window184 import parse_self_window, configured_action, queue_visibility
from jarvis.ui.desktop_events import drain_desktop_events, request_desktop


@pytest.mark.parametrize("phrase,action", [
    ("спрячься", "hide"), ("спрчься", "hide"), ("прячься", "hide"), ("скройся", "hide"),
    ("покажись", "show"), ("Покажись!", "show"),
    ("NEO, пожалуйста, спрячься", "hide"), ("пожалуйста, NEO, покажись", "show"),
    ("нео спрячься, пожалуйста", "hide"), ("Jarvis, покажись поверх всех окон", "show"),
    ("скрой окно ассистента", "hide"), ("покажи окно NEO", "show"),
    ("будь поверх всех окон", "show"),
])
def test_exact_raw_self_commands(phrase, action):
    assert parse_self_window(phrase, "ru") == action


@pytest.mark.parametrize("phrase", [
    "не спрячься", "не прячься", "не показывайся", "нео, не покажись", "пожалуйста не спрячься",
    '"спрячься"', "«покажись»", "'спрячься'", "что означает команда спрячься",
    "почему ты не понимаешь спрячься", "он сказал: покажись", "напиши спрячься",
    "создай заметку спрячься", "спрячься и удали файл", "спрячься\nпокажись",
    "спрячь активное окно", "скрой книжную полку", "скрой окно Chrome", "покажи", "скрой",
])
def test_discussion_negations_payloads_and_other_targets_do_not_match(phrase):
    assert parse_self_window(phrase, "ru") is None


@pytest.mark.parametrize("code,hide,show", [
    ("ru", "спрячься", "покажись"), ("en", "hide yourself", "show yourself"),
    ("de", "versteck dich", "zeig dich"), ("fr", "cache-toi", "montre-toi"),
    ("es", "escóndete", "muéstrate"), ("it", "nasconditi", "mostrati"),
    ("pt", "esconda-se", "mostre-se"),
])
def test_selected_locale_only(code, hide, show):
    assert parse_self_window(hide, code) == "hide"
    assert parse_self_window(show, code) == "show"
    assert parse_self_window(hide, "en" if code != "en" else "ru") is None


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        s = AtlasSession(tmp_path_factory.mktemp("self_window") / "session")
        yield s
        s.finish()


@pytest.fixture(autouse=True)
def clear_queue():
    drain_desktop_events(128)
    yield
    drain_desktop_events(128)


@pytest.mark.parametrize("source", ["text", "voice", "telegram", "ui", "hotkey"])
@pytest.mark.parametrize("phrase,action", [("спрячься", "hide"), ("покажись", "show")])
def test_real_agent_transports_bypass_llm_tools_and_normalization(session, monkeypatch, source, phrase, action):
    session.reset_case("ru")
    monkeypatch.setattr(session.agent, "_handle_stream_owned", lambda *a, **k: pytest.fail("Self-window command reached generic routing"))
    chunks = []
    turn = session.agent.handle_stream(phrase, input_source=source, conversation_id="qa", on_text=chunks.append)
    assert turn.success is True and turn.outcome == "queued"
    assert turn.tool_details == ["assistant.window." + action]
    assert "".join(chunks) == turn.text
    assert drain_desktop_events() == [{"action": "assistant.window." + action}]
    assert not session.calls


@pytest.mark.parametrize("source", ["ambient", "system_audio", "loopback", "unknown", "wake"])
def test_passive_and_unknown_sources_never_control_window(session, source):
    session.reset_case("ru")
    assert not session.agent.handle("спрячься", input_source=source).text
    assert not drain_desktop_events()


def test_disabled_or_full_queue_is_not_false_success(session):
    session.reset_case("ru")
    session.config.data["ui"]["voice_window_visibility_commands_enabled"] = False
    try:
        assert session.agent.handle("спрячься").success is False
        assert not drain_desktop_events()
    finally:
        session.config.data["ui"]["voice_window_visibility_commands_enabled"] = True
    for i in range(128):
        assert request_desktop("qa.inert", index=i)
    turn = session.agent.handle("покажись")
    assert turn.success is False and turn.outcome == "blocked"
    assert all(e["action"] == "qa.inert" for e in drain_desktop_events(128))


@pytest.mark.parametrize("source,key", [("text", "desktop"), ("voice", "desktop"), ("telegram", "telegram:qa")])
def test_pending_literal_body_remains_data(session, monkeypatch, source, key):
    from jarvis.agent_types import AgentTurn
    session.reset_case("ru")
    session.agent._activate_conversation(key)
    session.agent.conversations.get(key).pending_intent = SimpleNamespace(
        language="ru", expires_at=time.monotonic() + 60, missing=lambda: SimpleNamespace(kind="text"))
    received = []
    monkeypatch.setattr(session.agent, "_learned_meaning177", lambda *a: None)
    monkeypatch.setattr(session.agent, "_handle_stream_owned", lambda text, **kw: received.append(text) or AgentTurn("Text retained", success=True))
    monkeypatch.setattr(session.agent, "_typo_rescue", lambda *a: None)
    session.agent.handle("спрячься", input_source=source, conversation_id="qa")
    assert received == ["спрячься"] and not drain_desktop_events()


def test_real_telegram_adapter_uses_safe_queue(session):
    from jarvis.integrations.telegram_remote import TelegramRemoteController
    session.reset_case("ru")
    remote = object.__new__(TelegramRemoteController)
    remote.config = session.config
    remote._agent = session.agent
    remote._stop = threading.Event(); remote._stop.set()
    sent, api, notices = [], [], []
    remote._send_message = lambda chat, text: sent.append((chat, text)) or [123]
    remote._api = lambda *a, **k: api.append((a, k)) or {}
    remote._notify_desktop = notices.append
    for phrase, action in [("спрячься", "hide"), ("покажись", "show")]:
        remote._execute_agent_text("qa-chat", "qa-user", phrase)
        assert drain_desktop_events() == [{"action": "assistant.window." + action}]
    assert sent and notices and not session.calls


# Reuse the isolated real MainWindow fixture: no accounts, models, audio input,
# startup jobs, process launches or live desktop capture are ever available.
from tests.test_neo184_settings_mainwindow import app, window, settle


@pytest.mark.parametrize("topmost", [False, True])
def test_real_window_flags_and_repeated_hide_restore_preserve_identity(window, app, topmost, monkeypatch):
    from PySide6.QtCore import Qt, QTimer
    # A user-selected existing topmost mode is orthogonal to the one-time show
    # command. Establish it before capturing the HWND; commands never alter it.
    window.config.data["ui"]["always_on_top"] = topmost
    window.setWindowFlag(Qt.WindowStaysOnTopHint, topmost)
    window.show(); settle(app, .02)
    old_id, old_flags = int(window.winId()), window.windowFlags()
    saved_ui = copy.deepcopy(window.config.data["ui"])
    assert old_flags & Qt.WindowMinimizeButtonHint and old_flags & Qt.WindowSystemMenuHint
    assert old_flags & Qt.FramelessWindowHint
    assert bool(old_flags & Qt.WindowStaysOnTopHint) == topmost
    lowered = []
    original_lower = window.lower
    monkeypatch.setattr(window, "lower", lambda: (lowered.append(True), original_lower())[1])
    ticks = []
    listener = QTimer(window); listener.setInterval(5); listener.timeout.connect(lambda: ticks.append(1)); listener.start()
    for _ in range(3):
        assert window._set_runtime_topmost(False, hide=True)
        assert window._set_runtime_topmost(False, hide=True)
        settle(app, .02)
        assert window.isVisible() and not window.isMinimized() and not window._quitting and listener.isActive()
        assert window._set_runtime_topmost(True)
        assert window.isVisible() and not window.isMinimized()
        window.showMinimized(); settle(app, .02)
        assert window.isMinimized()
        assert window._set_runtime_topmost(True)
        assert not window.isMinimized()
        assert not window.testAttribute(Qt.WA_ShowWithoutActivating)
        assert not window._runtime_topmost and int(window.winId()) == old_id
        assert window.windowFlags() == old_flags
    assert len(lowered) == 6 and ticks and window.config.data["ui"] == saved_ui


@pytest.mark.parametrize("source", ["typed", "voice", "wake"])
def test_typed_voice_wake_adapters_without_mic(window, app, monkeypatch, source):
    rearms, lowered, raised = [], [], []
    original_lower, original_raise = window.lower, window.raise_
    monkeypatch.setattr(window, "lower", lambda: (lowered.append(True), original_lower())[1])
    monkeypatch.setattr(window, "raise_", lambda: (raised.append(True), original_raise())[1])
    monkeypatch.setattr(window, "_rearm_voice_after_answer", lambda: rearms.append(True))
    monkeypatch.setattr(window.agent, "handle_stream", lambda *a, **k: pytest.fail("Unexpected agent/LLM call"))
    for phrase, visible in [("спрячься", False), ("покажись", True)]:
        if source == "typed":
            window.input.setText(phrase); window._send_from_input()
        elif source == "voice":
            # Protect the original raw transcript even if a speech layer has
            # already canonicalized the command to an ambiguous generic verb.
            window._send_recognized_text("скрой" if not visible else "покажи", raw_text=phrase)
        else:
            window._voice_recognized("NEO " + phrase)
            settle(app, .04)
        assert window.isVisible() and not window.isMinimized()
    assert len(rearms) == 2 and len(lowered) == 1 and len(raised) == 1


def test_background_results_do_not_reopen_or_restore_window(window, app, monkeypatch):
    from jarvis.integrations.telegram_remote import TELEGRAM_DIALOGUE_MARKER
    raised = []
    monkeypatch.setattr(window, "raise_", lambda: raised.append(True))
    for minimized in (False, True):
        if minimized:
            window.show(); window.showMinimized()
        else:
            window._set_runtime_topmost(False, hide=True)
        window._append_assistant("Delayed result", paced=False)
        window._on_telegram_event(TELEGRAM_DIALOGUE_MARKER + "Remote command result")
        window._set_status("Background status")
        settle(app, .04)
        assert window.isMinimized() if minimized else window.isVisible()
        assert not raised


@pytest.mark.parametrize("phrase", ["не спрячься", "не покажись", "«спрячься»", "что означает команда покажись", "напиши спрячься"])
def test_real_agent_negative_discussion_and_dictation_never_touch_self(session, phrase):
    session.reset_case("ru")
    session.agent.handle(phrase)
    assert not any(e["action"].startswith("assistant.window.") for e in drain_desktop_events())


def test_acoustic_wake_normalizer_keeps_self_phrase_and_literal_ui_slot_does_not_hide(window, monkeypatch):
    from jarvis.ui.main_window import MainWindow
    from jarvis.runtime.conversation import ConversationStore
    assert window._normalize_recognized_command("спрячься") == "спрячься"
    assert window._normalize_recognized_command("покажись") == "покажись"
    window.agent.conversations = ConversationStore()
    window.agent.conversations.get("desktop").pending_intent = SimpleNamespace(
        language="ru", expires_at=time.monotonic() + 60, missing=lambda: SimpleNamespace(kind="text"))
    # Stop at the normal worker handoff. The GUI's local visibility shortcut
    # must not consume an opaque dictation body before that handoff.
    class RoutedLiteral(Exception): pass
    with monkeypatch.context() as patches:
        patches.setattr(window, "_cancel_command_asr157", lambda: (_ for _ in ()).throw(RoutedLiteral()))
        with pytest.raises(RoutedLiteral):
            window._send_text("спрячься", input_source="voice", raw_text="спрячься")
    assert window.isVisible()


@pytest.mark.parametrize("source", ["text", "voice", "telegram"])
def test_real_note_title_and_body_are_not_window_commands(session, source):
    session.reset_case("ru")
    session.agent.handle("создай заметку", input_source=source, conversation_id="note-qa")
    key = "telegram:note-qa" if source == "telegram" else "desktop"
    assert session.agent.conversations.get(key).pending_intent.missing().name == "title"
    session.agent.handle("спрячься", input_source=source, conversation_id="note-qa")
    assert session.agent.conversations.get(key).pending_intent.missing().name == "text"
    session.agent.handle("покажись", input_source=source, conversation_id="note-qa")
    assert len(session.calls) == 1
    assert session.calls[0]["tool"] == "shelf"
    assert session.calls[0]["arguments"]["title"] == "спрячься"
    assert session.calls[0]["arguments"]["text"] == "покажись"
    assert not drain_desktop_events()


def test_worker_thread_only_enqueues_gui_mutation(window, app, monkeypatch):
    results, lowered = [], []
    original_lower = window.lower
    monkeypatch.setattr(window, "lower", lambda: (lowered.append(True), original_lower())[1])
    t = threading.Thread(target=lambda: results.append(window._set_runtime_topmost(False, hide=True)))
    t.start(); t.join()
    assert results == [True] and window.isVisible() and not lowered
    window._drain_ui_commands()
    assert window.isVisible() and lowered == [True]
    assert queue_visibility("show")
    window._drain_ui_commands()
    assert window.isVisible()


def test_windows_raise_adapter_uses_own_hwnd_without_sticky_topmost(window, monkeypatch):
    from PySide6.QtCore import Qt
    import jarvis.ui.main_window as module
    native_calls = []
    api = SimpleNamespace(ShowWindow=lambda hwnd, mode: native_calls.append(("show", hwnd.value, mode)) or True,
                          SetWindowPos=lambda hwnd, after, x, y, w, h, flags: native_calls.append(("z", hwnd.value, after.value, flags)) or True)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=api), raising=False)
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    old_id = int(window.winId())
    assert window._set_runtime_topmost(True)
    assert native_calls == [("show", old_id, 8), ("z", old_id, None, 0x53)]
    assert not window.testAttribute(Qt.WA_ShowWithoutActivating)


def test_closing_and_disposed_windows_ignore_late_requests(window, app):
    from PySide6.QtWidgets import QWidget
    from jarvis.ui.main_window import MainWindow
    import shiboken6
    window._quitting = True
    try:
        assert window._set_runtime_topmost(True) is False
    finally:
        window._quitting = False
    disposed = QWidget(); shiboken6.delete(disposed)
    assert MainWindow._set_runtime_topmost(disposed, True) is False


@pytest.mark.parametrize('state_kind', ['clean', 'pending_alias', 'pending_confirmation'])
@pytest.mark.parametrize('phrase', ['Спрячься', 'спрчься', 'СПРЧЬСЯ!'])
def test_reported_hide_commands_win_before_clarification_or_confirmation(session, state_kind, phrase):
    session.reset_case('ru')
    state = session.agent.conversations.get('desktop')
    if state_kind == 'pending_alias':
        state.pending_until = time.monotonic() + 60
        state.pending_alias = {'phrase': 'спрячься', 'command': 'скрой книжную полку', 'language': 'ru'}
    elif state_kind == 'pending_confirmation':
        session.agent._pending_context = {'created_at': time.monotonic(), 'messages': [],
            'call': {'function': {'name': 'shelf', 'arguments': {'action': 'hide'}}}, 'tool_names': ['shelf']}
    turn = session.agent.handle(phrase)
    assert turn.success and turn.tool_details == ['assistant.window.hide']
    assert drain_desktop_events() == [{'action': 'assistant.window.hide'}]
    assert 'книжн' not in turn.text and not session.calls
    session.agent.handle('нет')
    assert not session.calls and not drain_desktop_events()


@pytest.mark.parametrize('phrase', ['не спрчься', '"спрчься"', 'напиши спрчься',
    'спрячьтесь', 'покажитесь', 'спрчься Chrome', 'покажись Firefox', 'спрячься и удали файл'])
def test_typos_do_not_widen_command_ownership(session, phrase):
    session.reset_case('ru')
    session.agent.handle(phrase)
    assert not any(e['action'].startswith('assistant.window.') for e in drain_desktop_events())


def test_actual_agent_queue_to_qt_hide_and_show_without_closing(window, app, monkeypatch):
    lowered, raised = [], []
    original_lower, original_raise = window.lower, window.raise_
    monkeypatch.setattr(window, 'lower', lambda: (lowered.append(True), original_lower())[1])
    monkeypatch.setattr(window, 'raise_', lambda: (raised.append(True), original_raise())[1])
    old_id, old_flags = int(window.winId()), window.windowFlags()
    from PySide6.QtCore import QTimer
    alive = QTimer(window); alive.start(1000)
    with monkeypatch.context() as m:
        m.setattr(window, 'hide', lambda: pytest.fail('Hide command must demote, not hide'))
        m.setattr(window, 'showMinimized', lambda: pytest.fail('Hide command must not minimize'))
        m.setattr(window, 'close', lambda: pytest.fail('Hide command must not quit'))
        m.setattr(window, 'activateWindow', lambda: pytest.fail('Visibility command must not steal focus'))
        for text, detail in [('спрчься', 'hide'), ('покажись', 'show'), ('Спрячься', 'hide')]:
            turn = window.agent.handle(text)
            assert turn.tool_details == ['assistant.window.' + detail]
            window._drain_ui_commands(); settle(app, .01)
            assert window.isVisible() and not window.isMinimized() and not window._quitting
            assert int(window.winId()) == old_id and window.windowFlags() == old_flags and alive.isActive()
    assert len(lowered) == 2 and len(raised) == 1


def test_windows_lower_adapter_removes_topmost_without_focus_or_state_change(window, monkeypatch):
    import jarvis.ui.main_window as module
    native_calls = []
    api = SimpleNamespace(SetWindowPos=lambda hwnd, after, x, y, w, h, flags:
        native_calls.append((hwnd.value, after.value, flags)) or True)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=api), raising=False)
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    old_id = int(window.winId())
    assert window._set_runtime_topmost(False, hide=True)
    assert native_calls == [(old_id, 1, 0x13)]  # HWND_BOTTOM; NOSIZE|NOMOVE|NOACTIVATE
    assert window.isVisible() and not window.isMinimized()
    api.SetWindowPos = lambda *a: False
    assert window._set_runtime_topmost(False, hide=True) is False


def test_rapid_visibility_sequence_and_theme_update_do_not_raise_demoted_window(window, app, monkeypatch):
    from PySide6.QtCore import Qt
    calls = []
    original_lower, original_raise, original_show = window.lower, window.raise_, window.show
    monkeypatch.setattr(window, 'lower', lambda: (calls.append('lower'), original_lower())[1])
    monkeypatch.setattr(window, 'raise_', lambda: (calls.append('raise'), original_raise())[1])
    monkeypatch.setattr(window, 'show', lambda: (calls.append('show'), original_show())[1])
    flags, old_id = window.windowFlags(), int(window.winId())
    for action in ('hide', 'show', 'hide'):
        assert queue_visibility(action)
    window._drain_ui_commands()
    assert calls == ['lower', 'show', 'raise', 'lower']
    assert window._runtime_demoted184 and not window._runtime_topmost
    calls.clear()
    window.config.data['ui']['accent_color'] = '#12ABEF'
    window.config.data['ui']['always_on_top'] = True
    window._apply_settings()
    settle(app, .04)
    assert not calls and window.isVisible() and not window.isMinimized()
    assert window.windowFlags() == flags and int(window.winId()) == old_id
    assert window.windowFlags() & Qt.WindowMinimizeButtonHint
    assert window._set_runtime_topmost(True)
    assert not window._runtime_demoted184 and calls == ['show', 'raise']


@pytest.mark.parametrize('alias', ['НЕЛ', 'Нело!', 'Наю?', 'нео…', 'NÉO.'])
def test_explicit_wake_address_preserves_raw_self_command(session, alias):
    session.reset_case('ru')
    turn = session.agent.handle(alias + ' спрчься')
    assert turn.tool_details == ['assistant.window.hide']
    assert drain_desktop_events() == [{'action': 'assistant.window.hide'}]
    assert not session.calls


def test_new_wake_prefix_flows_through_actual_voice_dispatch(window, app, monkeypatch):
    lower_calls = []
    original = window.lower
    monkeypatch.setattr(window, 'lower', lambda: (lower_calls.append(True), original())[1])
    monkeypatch.setattr(window, '_rearm_voice_after_answer', lambda: None)
    monkeypatch.setattr(window.agent, 'handle_stream', lambda *a, **k: pytest.fail('Visibility reached LLM path'))
    window._voice_recognized('Нело! спрчься')
    settle(app, .04)
    assert lower_calls == [True] and window.isVisible() and not window.isMinimized()


def test_hide_does_not_restore_an_already_hidden_or_minimized_window(window, app):
    window.hide()
    assert window._set_runtime_topmost(False, hide=True) and not window.isVisible()
    window.showMinimized(); settle(app, .02)
    assert window.isMinimized()
    assert window._set_runtime_topmost(False, hide=True) and window.isMinimized()


@pytest.mark.parametrize('code,hide,show,negative', [
    ('ru', 'спрячься', 'покажись', 'не спрячься'),
    ('en', 'hide yourself', 'show yourself', 'do not hide yourself'),
    ('de', 'versteck dich', 'zeig dich', 'versteck dich nicht'),
    ('fr', 'cache-toi', 'montre-toi', 'ne te cache pas'),
    ('es', 'escóndete', 'muéstrate', 'no te escondas'),
    ('it', 'nasconditi', 'mostrati', 'non nasconderti'),
    ('pt', 'esconda-se', 'mostre-se', 'não se esconda'),
])
def test_public_agent_preserves_all_languages_and_negation(session, code, hide, show, negative):
    for phrase, action in ((hide, 'hide'), (show, 'show')):
        session.reset_case(code)
        turn = session.agent.handle(phrase)
        assert turn.tool_details == ['assistant.window.' + action]
        assert drain_desktop_events() == [{'action': 'assistant.window.' + action}]
        assert not session.calls
    session.reset_case(code)
    session.agent.handle(negative)
    assert not any(e['action'].startswith('assistant.window.') for e in drain_desktop_events())


def test_visibility_hotkey_repeated_cycle_uses_demotion_state(window, app, monkeypatch):
    from PySide6.QtCore import Qt
    calls = []
    original_lower, original_raise = window.lower, window.raise_
    monkeypatch.setattr(window, 'lower', lambda: (calls.append('lower'), original_lower())[1])
    monkeypatch.setattr(window, 'raise_', lambda: (calls.append('raise'), original_raise())[1])
    monkeypatch.setattr(window, 'activateWindow', lambda: pytest.fail('Hotkey stole focus'))
    old_id, flags = int(window.winId()), window.windowFlags()
    for expected in (True, False, True, False, True):
        window._window_toggle_guard_until = 0
        window._toggle_window_topmost_hotkey()
        settle(app, .01)
        assert window._runtime_demoted184 is expected
        assert window.isVisible() and not window.isMinimized()
        assert (int(window.winId()), window.windowFlags()) == (old_id, flags)
        assert not window.testAttribute(Qt.WA_ShowWithoutActivating)
    assert calls == ['lower', 'raise', 'lower', 'raise', 'lower']
    # The existing key-repeat debounce must not accidentally undo the demotion.
    window._toggle_window_topmost_hotkey()
    assert calls == ['lower', 'raise', 'lower', 'raise', 'lower']


@pytest.mark.parametrize('initial', ['voice_hide', 'hidden', 'minimized', 'demoted_minimized'])
def test_visibility_hotkey_restores_each_background_state(window, app, monkeypatch, initial):
    from PySide6.QtCore import Qt
    if initial == 'voice_hide':
        window._try_local_voice_control('спрчься')
    elif initial == 'hidden':
        window.hide()
    else:
        if initial == 'demoted_minimized':
            window._set_runtime_topmost(False, hide=True)
        window.showMinimized()
    settle(app, .01)
    raised = []
    original_raise = window.raise_
    monkeypatch.setattr(window, 'raise_', lambda: (raised.append(True), original_raise())[1])
    monkeypatch.setattr(window, 'activateWindow', lambda: pytest.fail('Hotkey stole focus'))
    old_id, flags = int(window.winId()), window.windowFlags()
    window._window_toggle_guard_until = 0
    window._toggle_window_topmost_hotkey()
    settle(app, .01)
    assert raised == [True] and window.isVisible() and not window.isMinimized()
    assert not window._runtime_demoted184
    assert (int(window.winId()), window.windowFlags()) == (old_id, flags)
    assert not window.testAttribute(Qt.WA_ShowWithoutActivating)


@pytest.mark.parametrize('restore', ['tray', 'show_normal', 'show_hidden'])
def test_normal_restore_clears_demotion_before_next_hotkey(window, app, monkeypatch, restore):
    assert window._set_runtime_topmost(False, hide=True)
    if restore == 'tray':
        # Explicit tray restore retains its existing user-requested activation.
        window._show_from_tray()
    elif restore == 'show_normal':
        window.showMinimized(); settle(app, .01)
        assert window._runtime_demoted184
        window.showNormal()
    else:
        window.hide(); window.show()
    settle(app, .02)
    assert window.isVisible() and not window.isMinimized() and not window._runtime_demoted184
    calls = []
    original_lower = window.lower
    monkeypatch.setattr(window, 'lower', lambda: (calls.append('lower'), original_lower())[1])
    monkeypatch.setattr(window, 'activateWindow', lambda: pytest.fail('Hotkey stole focus'))
    window._window_toggle_guard_until = 0
    window._toggle_window_topmost_hotkey()
    assert calls == ['lower'] and window._runtime_demoted184


def test_existing_qt_activation_clears_stale_demotion_without_extra_raise(window, app, monkeypatch):
    from PySide6.QtWidgets import QWidget
    # This exercises Qt's activation event. Native taskbar behavior still needs
    # a Windows run; offscreen activation cannot establish native z-order.
    other = QWidget()
    try:
        other.show(); other.activateWindow(); settle(app, .02)
        assert window._set_runtime_topmost(False, hide=True)
        monkeypatch.setattr(window, 'raise_', lambda: pytest.fail('Activation observer re-raised'))
        window.activateWindow(); settle(app, .02)
        assert window.isActiveWindow() and not window._runtime_demoted184
    finally:
        other.close()
