"""NEO 188: historical records HIST-022 … (shared STT preload, command catalogue, Google draft, Discord ingress…)."""
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis.voice import TranscriptionService


class _Proc:
    def __init__(self):
        self.stdin, self.pid, self.killed = self, 1, False

    def write(self, line):
        pass

    def flush(self):
        pass

    def poll(self):
        return 1 if self.killed else None

    def kill(self):
        self.killed = True


def test_hist022_waiter_timeout_does_not_cancel_the_shared_preload(monkeypatch):
    service = TranscriptionService("t")
    proc = _Proc()
    monkeypatch.setattr(service, "_start_locked", lambda: setattr(service, "_process", proc))
    monkeypatch.setattr(service, "_cleanup_locked", lambda force: None)
    answered = threading.Event()

    class Responses:
        def get(self, timeout=None):
            if not answered.wait(timeout):
                import queue
                raise queue.Empty
            return {"id": request["id"], "ok": True, "text": ""}
    monkeypatch.setattr(service, "_responses", Responses())
    request = {"id": "preload-1", "cancel_serial": service._cancel_serial, "shared": True, "purpose": "preload"}
    result = []
    worker = threading.Thread(target=lambda: result.append(service._request_once_locked(request, 3.0)))
    worker.start()
    time.sleep(0.15)
    service.interrupt()                                   # a command waiting behind the preload timed out
    assert proc.killed is False                           # the shared work keeps running
    answered.set()
    worker.join(2)
    assert result == [""]


def test_hist022_interrupt_still_kills_an_owned_command_decode(monkeypatch):
    service = TranscriptionService("t")
    proc = _Proc()
    service._process = proc
    service._inflight188 = {"id": "c", "shared": False}
    service.interrupt()
    assert proc.killed is True


def test_hist022_waiter_behind_the_lock_is_cancelled_by_its_own_serial():
    service = TranscriptionService("t")
    serial = service._cancel_serial
    service.interrupt()
    with pytest.raises(TimeoutError):
        with service._request_lock(time.monotonic() + 1, serial):
            pass
    with service._request_lock(time.monotonic() + 1, None):    # shared work ignores waiter serials
        pass


def test_hist022_failed_switch_can_be_retried_with_the_same_choice():
    from jarvis.ui.main_window import MainWindow
    statuses = []
    window = SimpleNamespace(_quitting=False, _stt_switch_generation186=3, _stt_switch_key186=("large",),
                             _stt_switch_attempt188=(3, ("large",), ("small",)), _language_code=lambda: "ru",
                             _set_status=statuses.append)
    MainWindow._stt_switch_finished186(window, 3, "error", "large", "load failed")
    assert window._stt_switch_key186 == ("small",) and window._stt_switch_failed188 == ("large",)
    MainWindow._stt_switch_finished186(window, 2, "error", "large", "")   # an older switch never reports
    assert len(statuses) == 1


def test_hist023_gui_shortcut_never_builds_the_cold_catalogue(monkeypatch):
    from jarvis.runtime import command_repair161 as repair
    from jarvis.runtime import multilingual_commands as mc
    built, memo = [], {}
    real = repair._catalog_build

    def counted(code):
        if code not in memo:
            built.append(code)
            memo[code] = real(code)
        return memo[code]
    monkeypatch.setattr(repair, "_catalog_build", counted)
    monkeypatch.setattr(repair, "_READY", set())
    mc._real_word_cached.cache_clear()
    with mc.gui_fast_path():
        assert mc._real_word("калькулятор", "ru") is True        # kept as typed, nothing built
    assert built == [] and not repair.ready("ru")
    repair.catalog("ru")                                          # the background build
    assert repair.ready("ru") and built == ["ru"]
    with mc.gui_fast_path():
        assert mc._real_word("открйо", "ru") is False             # once ready, the real answer is used
    assert built == ["ru"]


def test_hist023_concurrent_first_callers_build_the_catalogue_once(monkeypatch):
    from jarvis.runtime import command_repair161 as repair
    calls = []

    def slow(code):
        calls.append(code)
        time.sleep(0.2)
        return ((), {}, frozenset(), frozenset())
    monkeypatch.setattr(repair, "_catalog_build", slow)
    monkeypatch.setattr(repair, "_READY", set())
    cache = {}                                   # memoize like the real lru-cached builder
    monkeypatch.setattr(repair, "_catalog_build", lambda code: cache[code] if code in cache else cache.setdefault(code, slow(code)))
    threads = [threading.Thread(target=repair.catalog, args=("de",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == ["de"] and repair.ready("de")


def test_hist024_derived_typo_indexes_follow_the_live_catalogue(tmp_path):
    import json
    from jarvis.runtime import canonical_commands, command_repair161, command_typos165
    pack = tmp_path / "plugin.json"
    pack.write_text(json.dumps({"plugin.zorblax": ["Запусти зорбликс", "Launch zorblaxer", "Starte zorblaxer",
                                                   "Lance zorblaxer", "Inicia zorblaxer", "Avvia zorblaxer",
                                                   "Inicia o zorblaxer"]}), encoding="utf-8")
    command_repair161.catalog("en"); command_typos165.vocabulary("en")              # warm English caches
    assert "zorblaxer" not in command_repair161.catalog("en")[2]
    version = canonical_commands.CATALOG_VERSION
    canonical_commands.register_extra_pack(pack)
    try:
        assert canonical_commands.CATALOG_VERSION == version + 1
        assert "zorblaxer" in command_repair161.catalog("en")[2]                    # warm English follows
        assert "zorblaxer" in command_typos165.vocabulary("en")
        assert "zorblaxer" in command_repair161.catalog("de")[2]                    # fresh German too
    finally:
        canonical_commands.unregister_extra_pack(pack)
    assert "zorblaxer" not in command_repair161.catalog("en")[2]                    # removal is followed as well
    assert "zorblaxer" not in command_typos165.vocabulary("en")


def test_hist025_failed_apply_keeps_the_google_token_draft(tmp_path, monkeypatch):
    from pathlib import Path
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    for key in ("HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.config import ConfigManager
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.ui import settings_dialog as sd
    monkeypatch.setattr(sd.SettingsDialog, "_refresh_microphones_async", lambda self: None)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: errors.append(a))
    for name in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: None)
    config = ConfigManager(Path(__file__).parents[1] / "config" / "default_settings.json")
    with external_io_blocked():
        dialog = sd.SettingsDialog(config)
        app.processEvents()
        dialog.gmail_client_id.setText("CLIENT_A")
        dialog._google_token_client184 = "CLIENT_A"
        dialog._gmail_tokens = {"refresh_token": "RG", "access_token": "AG", "expires_at": 1.0, "account_email": "a@x"}
        dialog._calendar_tokens = {"refresh_token": "RC", "access_token": "AC", "expires_at": 1.0}
        dialog.gmail_client_id.setText("CLIENT_B")                     # A → B
        real_save = config.save
        monkeypatch.setattr(config, "save", lambda data: (_ for _ in ()).throw(OSError("disk full")))
        dialog._save(close=False)                                      # the Apply fails before commit
        assert errors and dialog._gmail_tokens["refresh_token"] == "RG" and dialog._calendar_tokens["refresh_token"] == "RC"
        monkeypatch.setattr(config, "save", real_save)
        dialog.gmail_client_id.setText("CLIENT_A")                     # back B → A, retry without new authorization
        dialog._save(close=False)
        from jarvis.secrets import unprotect_secret
        assert unprotect_secret(config.get("integrations.gmail.refresh_token_encrypted")) == "RG"
        assert unprotect_secret(config.get("integrations.google_calendar.refresh_token_encrypted")) == "RC"
        dialog.gmail_client_id.setText("CLIENT_B")                     # a successful Apply with B does clear them
        dialog._save(close=False)
        assert not dialog._gmail_tokens["refresh_token"] and dialog._google_token_client184 == "CLIENT_B"
        dialog.reject()


def test_hist026_reset_keeping_connections_uses_the_newest_credentials(tmp_path, monkeypatch):
    from pathlib import Path
    for key in ("HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.config import ConfigManager
    from jarvis.secrets import protect_secret, unprotect_secret
    defaults = Path(__file__).parents[1] / "config" / "default_settings.json"
    window = ConfigManager(defaults)
    window.update(lambda d: d["integrations"]["gmail"].update(refresh_token_encrypted=protect_secret("OLD")), credentials=True)
    other = ConfigManager(defaults)                       # e.g. a token refresh in another component
    other.update(lambda d: d["integrations"]["gmail"].update(refresh_token_encrypted=protect_secret("NEW")), credentials=True)
    window.data["integrations"]["gmail"]["refresh_token_encrypted"] = protect_secret("OLD")   # stale memory snapshot
    window.data.setdefault("ui", {})["window_width"] = 777
    window.reset_to_defaults(keep_connections=True)
    fresh = ConfigManager(defaults)
    assert unprotect_secret(fresh.get("integrations.gmail.refresh_token_encrypted")) == "NEW"
    assert fresh.get("ui.window_width") != 777                                      # the rest is reset
    window.reset_to_defaults(keep_connections=False)
    assert not ConfigManager(defaults).get("integrations.gmail.refresh_token_encrypted")


def _bus_message(guild="G1", author="B1", bot=True):
    return SimpleNamespace(guild=SimpleNamespace(id=guild) if guild else None, channel=SimpleNamespace(id="C9"),
                           author=SimpleNamespace(id=author, bot=bot, display_name="NEO peer"))


def test_hist027_bus_packet_is_bound_to_its_actual_guild_and_client():
    from jarvis.integrations.background.discord_bot import bind_bus_ingress188 as bind
    ok = bind({"kind": "queue_probe", "guild_id": "G1"}, _bus_message(), "ME")
    assert ok["guild_id"] == "G1" and ok["_ingress_guild_id"] == "G1" and ok["_ingress_client_bot_id"] == "ME"
    assert ok["_message_author_bot_id"] == "B1" and ok["_ingress_channel_id"] == "C9"
    assert bind({"kind": "queue_probe", "guild_id": "OTHER"}, _bus_message(), "ME") is None     # declared ≠ actual
    assert bind({"kind": "queue_probe"}, _bus_message(guild=""), "ME") is None                   # DM never controls
    assert bind({"kind": "queue_probe"}, _bus_message(), "ME")["guild_id"] == "G1"               # missing → actual
    assert bind({"kind": "queue_probe"}, _bus_message(bot=False), "ME") is None                  # humans cannot forge
    assert bind({"kind": "ack", "target_bot_id": "X"}, _bus_message(), "ME") is None             # not addressed to us


def test_hist027_padded_kind_is_normalised_before_the_requester_check():
    from jarvis.integrations.background.discord_bot import bind_bus_ingress188 as bind
    assert bind({"kind": "  Request ", "requester_bot_id": "SOMEONE", "guild_id": "G1"}, _bus_message(), "ME") is None
    bound = bind({"kind": " REQUEST", "requester_bot_id": "B1", "guild_id": "G1"}, _bus_message(), "ME")
    assert bound["kind"] == "request"
