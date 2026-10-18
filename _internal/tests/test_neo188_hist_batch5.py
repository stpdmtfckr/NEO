"""NEO 188: historical records HIST-030 … (timer display, timer capture cancel, mail snapshot, voice close…)."""
import time
from types import SimpleNamespace

import pytest


def test_hist030_duration_shows_the_monotonic_projection_after_a_clock_jump(tmp_path, monkeypatch):
    from jarvis.runtime import timers
    manager = timers.TimerManager()
    manager.configure(tmp_path, "ru")
    monkeypatch.setattr(manager, "_ensure_thread_locked", lambda: None)
    item = manager.create(600, "чай")
    before = manager.display_due(item)
    assert abs(before - (time.time() + 600)) < 2
    real_time = time.time
    monkeypatch.setattr(timers.time, "time", lambda: real_time() + 3600)     # the wall clock moved +1 h
    shown = manager.display_due(item)
    assert abs(shown - (real_time() + 3600 + 600)) < 2                         # «now + remaining», not the stale due_at
    assert abs(item.due_at - before) < 2                                       # persisted estimate untouched
    alarm = manager.create_at(real_time() + 7200, "будильник")
    assert manager.display_due(alarm) == alarm.due_at                           # absolute alarm keeps its time


def test_hist030_listing_uses_the_projection(tmp_path, monkeypatch):
    from jarvis.runtime import everyday172, timers
    manager = timers.timer_manager(tmp_path, "ru")
    monkeypatch.setattr(manager, "_ensure_thread_locked", lambda: None)
    item = manager.create(600, "чай")
    monkeypatch.setattr(manager, "display_due", lambda timer, now=None: 0.0 if timer is item else timer.due_at)
    text = everyday172.listing_text([item], "ru")
    from datetime import datetime
    assert f"{datetime.fromtimestamp(0.0):%d.%m %H:%M}" in text
    manager.cancel(item.id)


def _delayed_agent(tmp_path, monkeypatch):
    from jarvis.runtime import delayed_actions184 as delayed, timers
    manager = timers.TimerManager()
    monkeypatch.setattr(manager, "_ensure_thread_locked", lambda: None)
    manager.configure(tmp_path, "en")
    monkeypatch.setattr(delayed, "_manager", lambda *_: manager)
    agent = SimpleNamespace(_conversation_key="desktop", _pending_context=None, conversations=None,
                            tools=SimpleNamespace(risk_for=lambda tool, args: 0),
                            _needs_confirmation=lambda tool, args, risk: False)
    return delayed, manager, agent


def test_hist031_cancel_during_target_capture_closes_the_admission(tmp_path, monkeypatch):
    from jarvis.runtime.cancellation import cancellation_manager
    delayed, manager, agent = _delayed_agent(tmp_path, monkeypatch)
    cancellation_manager().reset()

    def capture(action):
        cancellation_manager().cancel("stop")          # the user says «стоп» while the target is captured
        return {"label": "Video tab"}
    monkeypatch.setattr(delayed, "capture_target", capture)
    turn = delayed.handle(agent, "stop the video in 10 minutes", "en")
    assert turn is not None and turn.outcome == "cancelled" and manager.list() == []
    cancellation_manager().reset()
    monkeypatch.setattr(delayed, "capture_target", lambda action: {"label": "Video tab"})
    monkeypatch.setattr(delayed, "bound_video_tab", lambda target: object())
    monkeypatch.setattr(delayed, "resolve_target", lambda target: object())
    ok = delayed.handle(agent, "stop the video in 10 minutes", "en")
    assert ok.outcome == "scheduled" and len(manager.list()) == 1               # the normal control still works


def test_hist031_cancel_during_save_revalidation_closes_the_admission(tmp_path, monkeypatch):
    from jarvis.runtime.cancellation import cancellation_manager
    delayed, manager, agent = _delayed_agent(tmp_path, monkeypatch)
    cancellation_manager().reset()
    monkeypatch.setattr(delayed, "bound_video_tab", lambda target: cancellation_manager().cancel("stop") or object())
    schedule = {"seconds": 600, "label": "Stop video", "spec": {"action": "stop_video", "target": {}, "language": "en",
                                                               "risk": 0, "confirmed": True}}
    turn = delayed.save(agent, schedule)
    assert turn.outcome == "cancelled" and manager.list() == []
    cancellation_manager().reset()


def test_hist033_one_collection_cycle_reads_one_settings_snapshot(monkeypatch):
    from jarvis.integrations import mail_services as ms
    data = {"integrations": {"gmail": {"enabled": True, "include_inbox": True, "additional_folders": ["Work"]},
                             "mailru": {"enabled": False}}}
    config = SimpleNamespace(data=data, get=lambda k, d=None: d)
    seen = []

    class Client:
        def __init__(self, cfg):
            self.cfg = cfg

        def configured(self):
            return True

        def status(self, include_latest=5):
            first = ms.selected_mail_folders(self.cfg, "gmail")
            data["integrations"]["gmail"]["additional_folders"] = ["Changed"]      # Apply in the middle
            second = ms.selected_mail_folders(self.cfg, "gmail")
            seen.append((first, second))
            return SimpleNamespace(compact=lambda: {"provider": "gmail"})
    monkeypatch.setattr(ms, "GmailClient", Client)
    monkeypatch.setattr(ms, "provider_client", lambda cfg, provider: Client(cfg))
    result = ms.collect_statuses(config, include_latest=0)
    assert "gmail" in result and seen and seen[0][0] == seen[0][1]                 # one cycle, one view


def test_hist033_alerts_off_provider_is_not_polled_by_the_monitor(monkeypatch):
    from jarvis.integrations import mail_services as ms
    data = {"integrations": {"gmail": {"enabled": True, "notifications_enabled": False},
                             "mailru": {"enabled": True, "notifications_enabled": True}}}
    config = SimpleNamespace(data=data, get=lambda k, d=None: d)
    polled = []
    monkeypatch.setattr(ms, "enabled_providers", lambda cfg: ["gmail", "mailru"])
    monkeypatch.setattr(ms, "provider_client", lambda cfg, provider: polled.append(provider) or SimpleNamespace(
        status=lambda include_latest=5: SimpleNamespace(compact=lambda: {"provider": provider})))
    snapshot = ms.MailConfigSnapshot188(config)
    assert ms.notification_providers(snapshot) == ["mailru"]
    assert set(ms.collect_statuses(config, providers=["mailru"])) == {"mailru"} and polled == ["mailru"]
    polled.clear()
    assert set(ms.collect_statuses(config)) == {"gmail", "mailru"}                # a briefing still reads both


class _VoiceConfig:
    def __init__(self, root):
        self.paths = SimpleNamespace(root=root)
        self.data = {"response_voice": {"tier": "light", "enabled": True}}

    def get(self, key, default=None):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def test_hist034_close_failure_before_worker_start_releases_the_model_operation(tmp_path, monkeypatch):
    from jarvis.response_voice.service import VoiceService
    service = VoiceService(_VoiceConfig(tmp_path))
    calls = []
    monkeypatch.setattr(service, "cancel", lambda: (_ for _ in ()).throw(OSError("close failed")))
    monkeypatch.setattr(service, "_operate", lambda action, cancel: calls.append(action))
    assert service.model_operation("check") is False                     # the preparation failed …
    assert service._state["running"] is False and service._state["state"] == "prepare_failed"
    assert calls == []                                                     # … and no worker ran
    monkeypatch.setattr(service, "cancel", lambda: None)                   # the next attempt is admitted
    assert service.model_operation("check") is True
    service._operation_thread.join(2)
    assert calls == ["check"]
    service.close()


def test_hist034_late_cleanup_of_an_old_attempt_never_clears_a_newer_owner(tmp_path):
    import threading
    from jarvis.response_voice.service import VoiceService
    service = VoiceService(_VoiceConfig(tmp_path))
    old = threading.Event()
    service._operation_cancel = threading.Event()                          # a newer owner holds it
    service._state["running"] = True
    assert service._publish_worker(operation=old, terminal=True, running=False) is False
    assert service._state["running"] is True
    service._state["running"] = False
    service.close()
