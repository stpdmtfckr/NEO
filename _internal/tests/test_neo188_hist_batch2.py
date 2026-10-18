"""NEO 188: historical records HIST-006 … (scheduled commands, media admission, overlays, GameWeb, games, focus)."""
import time
from types import SimpleNamespace

import pytest

from jarvis.runtime.dialogue_tasks import PendingIntent, Slot
from jarvis.ui.main_window import MainWindow


class _Timers:
    def __init__(self, events):
        self.events = list(events)

    def list(self):
        return []

    def drain(self, limit):
        events, self.events = self.events, []
        return events


class _Conversations:
    def __init__(self):
        self.state = SimpleNamespace(pending_intent=None)

    def get(self, key):
        return self.state


def _window(agent, events):
    sent = []
    window = SimpleNamespace(_timer_manager=_Timers(events), _busy=False, _quitting=False, agent=agent,
                             _language_code=lambda: "ru")
    window._send_text = lambda text, **kw: sent.append((text, kw))
    return window, sent


@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_hist006_scheduled_command_never_fills_the_open_note_slot(code):
    agent = SimpleNamespace(conversations=_Conversations(), _pending_context=None, _pending_target_choice=None)
    agent.conversations.state.pending_intent = PendingIntent(
        "shelf.create", {}, (Slot("text", "text", "text"),), code, time.monotonic() + 60)
    window, sent = _window(agent, [SimpleNamespace(text="открой калькулятор", kind="command")])
    window._language_code = lambda: code
    MainWindow._drain_timer_events(window)
    assert sent == [] and agent.conversations.state.pending_intent is not None    # the slot stays the user's
    agent.conversations.state.pending_intent = None                                 # the user answered/cancelled
    MainWindow._drain_timer_events(window)
    assert [text for text, _ in sent] == ["открой калькулятор"] and sent[0][1]["user_submission"] is False


def test_hist006_expired_question_and_confirmation_rules():
    agent = SimpleNamespace(conversations=_Conversations(), _pending_context=None, _pending_target_choice=None)
    agent.conversations.state.pending_intent = PendingIntent(
        "shelf.create", {}, (Slot("text", "text", "text"),), "ru", time.monotonic() - 1)
    window, sent = _window(agent, [SimpleNamespace(text="напомни выпить воды", kind="command")])
    MainWindow._drain_timer_events(window)
    assert len(sent) == 1                                     # an expired question no longer holds the queue
    agent._pending_context = {"tool": "x"}
    window, sent = _window(agent, [SimpleNamespace(text="открой блокнот", kind="command")])
    MainWindow._drain_timer_events(window)
    assert sent == []                                         # a pending confirmation is never answered by a timer


def _media_window():
    return SimpleNamespace(_quitting=False, _busy=True, _continuous_voice_session=False, _listening_bubble=None,
                           _schedule_wake_watchdog=lambda ms: None, _language_code=lambda: "ru",
                           _quick_media_transport_inflight=True, _quick_media_transport_action="next",
                           _quick_media_transport_token188=2)


def test_hist007_old_callback_never_releases_the_newer_transport_admission():
    window = _media_window()                                   # C («дальше», serial 2) holds the admission
    MainWindow._quick_media_failed(window, "boom", serial=1)   # late error of an older pause B
    assert window._quick_media_transport_inflight is True and window._quick_media_transport_action == "next"
    MainWindow._quick_media_failed(window, "boom", serial=2)   # C's own callback releases it
    assert window._quick_media_transport_inflight is False and window._quick_media_transport_token188 is None


def test_hist007_callbacks_without_serial_keep_the_old_contract():
    from jarvis.ui.main_window import _release_quick_media_transport188
    window = _media_window()
    _release_quick_media_transport188(window, None)
    assert window._quick_media_transport_inflight is False


def _qt_app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _spin(ms):
    from PySide6.QtCore import QElapsedTimer
    from PySide6.QtWidgets import QApplication
    clock = QElapsedTimer(); clock.start()
    while clock.elapsed() < ms:
        QApplication.processEvents()


def test_hist008_disabled_music_overlay_stops_reading_and_building_icons(monkeypatch):
    _qt_app()
    from jarvis.ui import overlay_manager
    reads, icons = [], []
    real_icon = overlay_manager._music_icon
    monkeypatch.setattr(overlay_manager, "_music_icon", lambda *a, **k: icons.append(a) or real_icon(*a, **k))
    panel = overlay_manager.MusicOverlayPanel("music", state_provider=lambda: reads.append(1) or {"title": "Song", "playing": False})
    panel.apply_model({"visible": True, "style": {}})
    _spin(600)
    assert len(reads) >= 2 and len(icons) == 3              # polling works; same glyphs are not rebuilt
    panel.suspend()
    reads.clear(); icons.clear()
    _spin(600)
    assert reads == [] and icons == [] and not panel.isVisible()
    panel.apply_model({"visible": True, "style": {}})          # shown again: fresh data at once, poll resumes
    assert reads and panel._music_timer.isActive()
    panel.close()


class _Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_hist009_settings_apply_keeps_the_open_game_question(tmp_path, monkeypatch):
    from jarvis.game_web import service as gw
    monkeypatch.setattr(gw, "SearchAPIGoogleProvider", lambda config: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(gw, "GameWebCache", lambda config: object())
    monkeypatch.setattr(gw, "GameWebContextStore", lambda config: object())
    monkeypatch.setattr(gw, "GameManager", lambda **kw: object())
    old = gw.GameWebKnowledgeService(_Cfg(), ollama=None)
    old._missing_game = ("сколько стоит меч", time.monotonic() + 90)
    old._conversation.game_name = "Elden Ring"
    old._conversation.pending_followup = "Где его найти?"
    fresh = gw.GameWebKnowledgeService(_Cfg(), ollama=None)
    assert fresh.pending_game_title("ru") is None                         # the old bug: a fresh service is empty
    assert fresh.adopt_dialogue(old) is True
    assert fresh.pending_game_title("ru")[0] == "сколько стоит меч"
    assert fresh._conversation.game_name == "Elden Ring" and fresh._conversation.pending_followup == "Где его найти?"
    off = gw.GameWebKnowledgeService(_Cfg({"gaming.enabled": False}), ollama=None)
    assert off.adopt_dialogue(old) is False and off.pending_game_title("ru") is None


def test_hist009_agent_refresh_moves_the_dialogue(monkeypatch):
    import jarvis.agent as agent_module
    closed = []

    class Fresh:
        def __init__(self, config, **kw):
            self.adopted = None

        def adopt_dialogue(self, previous):
            self.adopted = previous
    old = SimpleNamespace(close=lambda: closed.append(1))
    monkeypatch.setattr(agent_module, "GameWebKnowledgeService", Fresh)
    monkeypatch.setattr(agent_module, "PhraseAtlas2", lambda *a, **k: None)
    monkeypatch.setattr(agent_module, "DomainDispatcher", lambda *a, **k: None)
    monkeypatch.setattr(agent_module, "LazyVisionGate", lambda *a, **k: None)
    monkeypatch.setattr(agent_module, "resource_budget", lambda: SimpleNamespace(configure=lambda c: None))
    agent = SimpleNamespace(config=_Cfg(), ollama=SimpleNamespace(refresh_model_settings=lambda: None),
                            atlas2=SimpleNamespace(compatible_with=lambda *a, **k: True), context_memory=None,
                            router=None, game_web=old, _active_game_id=lambda: "")
    try:
        agent_module.Agent.refresh_runtime_settings(agent)
    except Exception:
        pass                                                   # later refresh layers are out of scope here
    assert isinstance(agent.game_web, Fresh) and agent.game_web.adopted is old and closed == [1]


def _tracker(monkeypatch):
    import jarvis.gaming.memory as memory
    published = []
    monkeypatch.setattr(memory, "record_active_game", lambda **kw: published.append(kw.get("pid")))
    monkeypatch.setattr(memory, "clear_active_game", lambda **kw: None)
    monkeypatch.setattr(memory, "GameWebContextStore", lambda config: SimpleNamespace(record=lambda *a, **k: None))
    notices, replay = [], []
    cfg = _Cfg({"streaming.enabled": False})
    t = memory.GamingContextTracker(cfg, SimpleNamespace(enabled=lambda: False), notice_callback=notices.append)
    t._replay_watch = SimpleNamespace(game_started=lambda name, **kw: replay.append(name), game_ended=lambda: None,
                                      poll=lambda: None)
    return t, notices, replay, published


def _snap(pid=4242):
    return {"game_name": "Dota 2", "pid": pid, "session_started_at": time.time() - 30, "foreground_verified": False}


def test_hist010_detection_path_started_before_stop_admits_nothing(monkeypatch):
    t, notices, replay, published = _tracker(monkeypatch)
    t._generation188 += 1                                   # the poll loop of a running tracker
    generation = t._generation188
    t.stop()
    t._observe_snapshot(_snap(), generation)               # its snapshot arrives after stop() returned
    assert notices == [] and replay == [] and published == [] and t._current is None


def test_hist010_old_path_cannot_act_for_a_restarted_tracker(monkeypatch):
    t, notices, replay, _ = _tracker(monkeypatch)
    old = t._generation188
    t.stop()
    t._stop.clear(); t._generation188 += 1                 # start() of a new owner
    t._observe_snapshot(_snap(), old)
    assert replay == []
    t._observe_snapshot(_snap(), t._generation188)          # the new owner's own path works normally
    assert replay == ["Dota 2"] and notices and "Dota 2" in notices[0]


def test_hist011_settings_menu_tab_order_follows_the_visual_groups():
    _qt_app()
    from PySide6.QtWidgets import QLabel
    from jarvis.ui.settings_dialog import ScrollableSettingsTabs
    tabs = ScrollableSettingsTabs()
    for n in range(4):
        tabs.addTab(QLabel(f"page {n}"), f"P{n}")
    tabs.set_navigation_groups186([("A", (2, 0)), ("B", (3, 1))])
    visual = [tabs._tab_layout.itemAt(i).widget() for i in range(tabs._tab_layout.count())]
    visual = [w for w in visual if w is not None]
    assert [w.text() for w in visual] == ["A", "P2", "P0", "B", "P3", "P1"]

    def walk(start, step, count):
        out, node = [], start
        for _ in range(count):
            node = getattr(node, step)()
            out.append(node)
        return out
    forward = walk(tabs._search, "nextInFocusChain", len(visual))
    assert forward == visual and walk(tabs._search, "nextInFocusChain", len(visual) + 1)[-1] is tabs._subtabs187
    backward = walk(tabs._subtabs187, "previousInFocusChain", len(visual))
    assert backward == list(reversed(visual))                   # Shift+Tab is symmetric
    tabs.replaceTab(1, QLabel("late page"))                      # deferred page replacement keeps the order
    assert walk(tabs._search, "nextInFocusChain", len(visual)) == visual
    tabs.deleteLater()
