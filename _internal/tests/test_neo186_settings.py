"""186: settings / UI contracts (numeric round-trip, heavy voice store, UI copy)."""
from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ------------------------------------------------------------ SETTINGS185-RANGE-01

@pytest.mark.parametrize("key,value", [
    ("voice.silence_seconds", 1.7), ("voice.silence_seconds", 1.05), ("voice.silence_seconds", 0.45),
    ("wake_word.chunk_seconds", 0.8), ("wake_word.chunk_seconds", 4.5), ("wake_word.silence_seconds", 0.12),
    ("wake_word.silence_seconds", 0.33), ("wake_word.silence_seconds", 0.95),
])
def test_voice_numbers_round_trip_through_save_and_load(tmp_path, monkeypatch, key, value):
    from jarvis.config import ConfigManager
    for name in ("HOME", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(ConfigManager, "apply_wake_service", lambda *_: None)
    monkeypatch.setattr(ConfigManager, "apply_autostart", lambda *_: None)
    config = ConfigManager(ROOT / "config/default_settings.json")
    section, field = key.split(".")
    config.data[section][field] = value
    config.save(config.data)
    assert config.get(key) == value
    assert ConfigManager(ROOT / "config/default_settings.json").get(key) == value


def test_widget_save_and_load_share_one_range():
    from PySide6.QtWidgets import QApplication, QDoubleSpinBox
    from jarvis.runtime.numeric_schema186 import VOICE_NUMBERS, clamp, configure_spinbox
    QApplication.instance() or QApplication([])
    for key, (low, high, decimals, _step, _default) in VOICE_NUMBERS.items():
        box = QDoubleSpinBox()
        configure_spinbox(box, key)
        assert (box.minimum(), box.maximum(), box.decimals()) == (low, high, decimals)
        box.setValue(high + 5)
        assert clamp(key, box.value()) == box.value() == high
        box.setValue(low - 5)
        assert clamp(key, box.value()) == box.value() == low
    assert clamp("voice.silence_seconds", 1.05) == 1.05 and clamp("wake_word.chunk_seconds", 0.95) == 0.95


# ------------------------------------------------------------ NEW185-TTS-01/02/03

def _hub():
    import importlib.util
    spec = importlib.util.spec_from_file_location("voice_tiers185", ROOT / "tests" / "test_neo185_voice_tiers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FakeHub()


def test_cancelled_heavy_download_makes_no_request(tmp_path):
    from jarvis.response_voice.model_store import Cancelled, HeavyModelStore
    hub = _hub()
    cancel = threading.Event(); cancel.set()
    with pytest.raises(Cancelled):
        HeavyModelStore(tmp_path, transport=hub).download(cancel, explicit=True)
    assert hub.urls == []
    assert not (tmp_path / "models" / "qwen-response-voice-1.7b.manifest.json").exists()


def test_busy_install_lock_blocks_before_manifest_request(tmp_path):
    from jarvis.response_voice.model_store import HeavyModelStore, VoiceModelError
    hub = _hub()
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "response_voice.install.lock").write_text("12345", encoding="ascii")
    with pytest.raises(VoiceModelError):
        HeavyModelStore(tmp_path, transport=hub).download(threading.Event(), explicit=True)
    assert hub.urls == []
    assert not (tmp_path / "models" / "qwen-response-voice-1.7b.manifest.json").exists()
    assert (tmp_path / "response_voice.install.lock").read_text(encoding="ascii") == "12345"   # not stolen


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_temporary_manifest_never_follows_a_planted_link(tmp_path):
    from jarvis.response_voice.model_store import HeavyModelStore
    models = tmp_path / "models"; models.mkdir(parents=True)
    sentinel = tmp_path / "sentinel.txt"; sentinel.write_text("untouched", encoding="utf-8")
    try:
        os.symlink(sentinel, models / "qwen-response-voice-1.7b.manifest.tmp")
    except OSError:
        pytest.skip("symlink creation not permitted")
    HeavyModelStore(tmp_path, transport=_hub()).resolve()
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert json.loads((models / "qwen-response-voice-1.7b.manifest.json").read_text(encoding="utf-8"))["revision"]


def test_heavy_download_still_completes(tmp_path):
    from jarvis.response_voice.model_store import HeavyModelStore
    result = HeavyModelStore(tmp_path, transport=_hub()).download(threading.Event(), explicit=True)
    assert result["state"] == "verified"
    assert not (tmp_path / "response_voice.install.lock").exists()


# ------------------------------------------------------------ UI copy and i18n

@pytest.mark.parametrize("code", ["en", "de", "fr", "es", "it", "pt"])
def test_wake_and_window_help_keep_their_meaning(code):
    import re
    from jarvis.i18n import tr
    for source in ("Главная фраза активации. После неё можно сразу произнести команду.",
                   "Можно оставить только текст/контуры элементов.", "Ширина основного окна в пикселях."):
        text = tr(source, code)
        assert not re.search(r"[А-Яа-яЁё]", text) and len(re.findall(r"[^\W\d_]{2,}", text)) >= 5, text


@pytest.mark.parametrize("code,word", [("fr", "Longueur"), ("es", "Longitud"), ("it", "Lunghezza"), ("pt", "Comprimento")])
def test_remote_command_limit_is_a_length(code, word):
    from jarvis.i18n import tr
    assert tr("Максимальная длина команды", code).startswith(word)


def test_portuguese_examples_match_the_features():
    from jarvis.runtime.quick_capture165 import parse_quick_capture
    from jarvis.runtime.spoken_spelling165 import correct_transcript
    catalog = json.loads((ROOT / "jarvis/locales/ui/pt.json").read_text(encoding="utf-8"))["strings"]
    spelling = catalog["Исправлять неверно услышанные слова («превет» → «привет»)"]
    assert "«obrigdo» → «obrigado»" in spelling
    assert correct_transcript("obrigdo", "pt", wait=True)[0] == "obrigado"
    quick = catalog["Быстрые команды без wake word (клип / запись / стоп запись / скриншот)"]
    example = quick.split("(", 1)[1].rstrip(")").split(" / ")[-1]
    assert parse_quick_capture(example, "pt").action == "screenshot"


def test_short_caption_fallback_never_returns_a_fragment():
    from jarvis.i18n import GENERIC_HELP, tr
    text = tr("Можно оставить совсем другое неизвестное.", "en")
    assert text == GENERIC_HELP["en"] or ("." not in text.strip(".") and len(text.split()) >= 2)


# ------------------------------------------------------------ UI185-OVERLAY-LITERAL-SESSION-I18N-01

def test_ordinary_note_starting_with_session_stays_literal():
    from jarvis.ui.overlay_manager import _localized_dynamic_text
    note = {"text": "Сессия: 5 мин с бабушкой\nкупить хлеб", "binding": {}}
    assert _localized_dynamic_text(note, "en") == note["text"]
    foreign = {"text": "Сессия: 10 мин", "binding": {"type": "weather"}}
    assert _localized_dynamic_text(foreign, "de") == foreign["text"]


def test_application_session_timing_is_localized():
    from jarvis.ui.overlay_manager import _localized_dynamic_text
    panel = {"text": "Игра: QA\nСессия: 0 с", "binding": {"type": "game_session", "status": "ended", "session_seconds": 65}}
    text = _localized_dynamic_text(panel, "en")
    assert "Session: 1 min 05 s" in text and "Игра: QA" in text


# ------------------------------------------------------------ UI185-GIF-DIALOG-STATE-A11Y-01

@pytest.mark.parametrize("code", ["ru", "en"])
def test_gif_dialog_controls_name_their_state(tmp_path, code):
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton
    from jarvis.ui.desktop_strings import dt
    from jarvis.ui.gif_settings import GifSettingsDialog
    from jarvis.ui.robot_widget import RobotWidget
    QApplication.instance() or QApplication([])
    dialog = GifSettingsDialog(tmp_path, {"idle": "user_file.gif"}, 100, code)
    try:
        assert dialog.scale.accessibleName() == dt("scale", code) and dialog.scale.accessibleDescription()
        buddies = {label.buddy() for label in dialog.findChildren(QLabel) if label.buddy() is not None}
        assert dialog.scale in buddies
        edits = [w for w in dialog.findChildren(QLineEdit) if w.isReadOnly()]
        assert len(edits) == len(RobotWidget.STATES) and all(edit in buddies for edit in edits)
        for state in RobotWidget.STATES:
            name = dt("state_" + state, code)
            assert any(name in edit.accessibleName() for edit in edits)
        buttons = [b for b in dialog.findChildren(QPushButton) if b.text() in {dt("load", code), dt("clear", code)}]
        assert len(buttons) == 2 * len(RobotWidget.STATES)
        assert len({b.accessibleName() for b in buttons}) == len(buttons)        # each names its state
        idle = next(e for e in edits if e.text() == "user_file.gif")
        assert "user_file.gif" not in idle.accessibleName()                    # the file name is not the label
    finally:
        dialog.deleteLater()


# ------------------------------------------------------------ SETTINGS185-RECOVERY-CONCURRENT-SAVE-01

def _settings_files(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"{broken")
    (tmp_path / "settings.last-good.json").write_text('{"v": "OLD"}', encoding="utf-8")
    defaults = tmp_path / "defaults.json"
    defaults.write_text("{}", encoding="utf-8")
    return path, defaults


def test_recovery_keeps_a_newer_save_written_after_the_failed_read(tmp_path):
    from jarvis.settings_storage184 import atomic_write, recover_settings
    path, defaults = _settings_files(tmp_path)
    observed = path.read_bytes()
    atomic_write(path, b'{"v": "NEW"}')                       # a later successful Save
    backup, source = recover_settings(path, tmp_path, defaults, observed=observed, check_generation=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": "NEW"} and source == str(path) and backup == ""


def test_recovery_rechecks_the_generation_right_before_restore(tmp_path, monkeypatch):
    import jarvis.settings_storage184 as storage
    path, defaults = _settings_files(tmp_path)
    observed = path.read_bytes()
    real = storage.valid_settings

    def writer_between(source):
        # The Save lands after quarantine, while recovery inspects its source.
        if source.name == "settings.last-good.json" and path.read_bytes() == observed:
            storage.atomic_write(path, b'{"v": "NEW"}')
        return real(source)
    monkeypatch.setattr(storage, "valid_settings", writer_between)
    backup, source = storage.recover_settings(path, tmp_path, defaults, observed=observed, check_generation=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": "NEW"} and source == str(path)
    assert Path(backup).read_bytes() == observed                # damaged bytes are still kept


def test_sequential_recovery_still_restores_last_good(tmp_path):
    from jarvis.settings_storage184 import recover_settings
    path, defaults = _settings_files(tmp_path)
    observed = path.read_bytes()
    backup, source = recover_settings(path, tmp_path, defaults, observed=observed, check_generation=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": "OLD"} and source.endswith("settings.last-good.json")
    assert Path(backup).read_bytes() == observed


def test_recovery_inside_a_protected_save_does_not_deadlock(tmp_path):
    from jarvis.config import _OwnedProfileLock186
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    with _OwnedProfileLock186(path):
        with _OwnedProfileLock186(path):      # nested owner reuse, no second file lock
            pass
    with _OwnedProfileLock186(path):          # released properly
        pass


# ------------------------------------------------------------ TIMER185-DURATION-WALL-CLOCK-SOURCE

def test_duration_timer_ignores_wall_clock_jumps(monkeypatch):
    import time as real_time
    from jarvis.runtime import timers
    manager = timers.TimerManager()
    monkeypatch.setattr(manager, "_ensure_thread_locked", lambda: None)
    wall = [real_time.time()]
    monkeypatch.setattr(timers.time, "time", lambda: wall[0])
    item = manager.create(600, "QA")
    for jump in (3600, -3600):
        wall[0] += jump
        assert 598 <= manager.remaining(item) <= 600
    alarm = manager.create_at(wall[0] + 600, "Alarm QA", "alarm")
    wall[0] += 300
    assert 295 <= manager.remaining(alarm) <= 300          # absolute targets keep wall-clock meaning


# ------------------------------------------------------------ UI185-SEARCH-DEFERRED-HIT-OWNERSHIP-01

def test_deferred_search_hit_is_dropped_after_the_query_changes():
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
    from jarvis.ui.settings_dialog import ScrollableSettingsTabs
    QApplication.instance() or QApplication([])
    tabs = ScrollableSettingsTabs()
    pages = []
    for title, caption in (("Alpha", "User name"), ("Beta", "Something else")):
        page = QWidget(); QVBoxLayout(page).addWidget(QLabel(caption))
        tabs.addTab(page, title); pages.append(page)
    label = pages[0].findChild(QLabel)
    tabs._search.setText("user")
    ticket = (tabs._search_rev186, 0, tabs._stack.widget(0), tabs._search.text())
    tabs._search.setText("zzz-no-match")                   # interruption before delivery
    tabs._show_deferred_hit186(label, ticket)
    assert tabs._hit_widget is None and not label.property("searchHit")
    tabs._search.setText("user")
    ticket = (tabs._search_rev186, 0, tabs._stack.widget(0), tabs._search.text())
    tabs._show_deferred_hit186(label, ticket)              # a matching, current callback still works
    assert tabs._hit_widget is label
    tabs._clear_hit()
    replacement = QWidget(); QVBoxLayout(replacement).addWidget(QLabel("User name"))
    tabs.replaceTab(0, replacement)
    tabs._show_deferred_hit186(label, ticket)              # the old page was replaced
    assert tabs._hit_widget is None


# ------------------------------------------------------------ STT185-CACHE-01

def test_stt_cache_frees_idle_models_before_building_a_new_one(monkeypatch, capsys):
    import sys, types
    from jarvis import stt_worker
    alive: list[str] = []

    class FakeModel:
        def __init__(self, name, **_kw):
            if len(alive) >= 2:
                raise MemoryError("budget of two models")
            self.name = name
            alive.append(name)

        def __del__(self):
            alive.remove(self.name)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    requests = [{"cmd": "x", "id": str(i), "purpose": "preload", "model_name": name, "device": "cpu", "compute_type": "int8"}
                for i, name in enumerate(("small", "tiny", "large-v3-turbo", "large-v3-turbo"))]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(r) + "\n" for r in requests)))
    stt_worker.main()
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip().startswith("{")]
    assert [r["ok"] for r in replies[-4:]] == [True, True, True, True], replies


# ------------------------------------------------------------ REDACT185-*

def test_known_key_is_removed_before_the_detail_is_clipped():
    from jarvis.game_web.searchapi import _safe_detail
    key = "QAFAKEKEY_" + "x" * 30

    class Response:
        def __init__(self, body):
            self.body = body
            self.text = json.dumps(body)

        def json(self):
            return self.body
    for pad in (190, 200, 210, 219):
        for body in ({"error": "a" * pad + key}, {"error": {"message": "b" * pad + key}}):
            detail = _safe_detail(Response(body), secrets=(key,))
            assert "QAFAKEKEY" not in detail and len(detail) <= 220


def test_shared_redactor_masks_bot_urls_bearer_suffix_and_spaced_label():
    from jarvis.runtime.diagnostics import redact, report_bytes, safe_error
    token = "123456789:QAFAKEbotTOKENxxxxxxxxxxxxxxxxxx"
    for text in (f"POST https://api.telegram.org/bot{token}/getMe -> 401", f"standalone {token}"):
        out = redact(text)
        assert "QAFAKEbot" not in out
    assert "api.telegram.org" in redact(f"https://api.telegram.org/bot{token}/getMe") and "getMe" in redact(f"/bot{token}/getMe")
    assert "QAFAKEbot" not in report_bytes({"detail": f"https://api.telegram.org/bot{token}/x"}).decode("utf-8")
    assert "SUFFIXQA" not in redact("Authorization failed: Bearer abc.DEF~SUFFIXQA then status 401")
    assert "status 401" in redact("Authorization failed: Bearer abc.DEF~SUFFIXQA then status 401")
    assert "MARKERQA" not in redact("api key: MARKERQA") and "MARKERQA" not in safe_error(RuntimeError("api key=MARKERQA"))
    assert redact("first_token_timeout_seconds=20 and plain words stay") == "first_token_timeout_seconds=20 and plain words stay"


# ------------------------------------------------------------ copy / control contracts

def test_scenario_run_hint_names_the_actual_blocker():
    from jarvis.ui.scenario_strings import st
    source = (ROOT / "jarvis/ui/scenario_editor.py").read_text(encoding="utf-8")
    assert '"enable_first"' in source and '"fix_invalid"' in source
    for code in ("ru", "en", "de", "fr", "es", "it", "pt"):
        assert st("enable_first", code) != st("save_first", code)


def test_zero_bubble_opacity_has_no_resting_fill():
    source = (ROOT / "jarvis/ui/main_window.py").read_text(encoding="utf-8")
    assert "max(8, bubble_opacity // 5) if bubble_opacity else 0" in source


def test_combined_mail_list_respects_count_only():
    source = (ROOT / "jarvis/tools/extended.py").read_text(encoding="utf-8")
    assert 'mode = "sender_subject"' not in source.split("def _mail_mixed_rows_text", 1)[1].split("def ", 1)[0]


def test_translated_gmail_examples_keep_their_built_in_meaning():
    from jarvis.integrations.mail_services import GmailClient
    catalog = {code: json.loads((ROOT / f"jarvis/locales/ui/{code}.json").read_text(encoding="utf-8"))["strings"]
               for code in ("ru", "en", "de", "fr", "es", "it", "pt")}
    for code, strings in catalog.items():
        projects, newsletters, self_sent = [x.strip() for x in strings["Проекты; Рассылки; Отправленные себе"].split(";")]
        assert GmailClient._folder_query(newsletters) == "category:promotions", code
        assert GmailClient._folder_query(self_sent) == "from:me to:me", code
        assert GmailClient._folder_query(projects).startswith("label:"), code


def test_calendar_month_option_says_31_days():
    for code in ("en", "de", "fr", "es", "it", "pt"):
        strings = json.loads((ROOT / f"jarvis/locales/ui/{code}.json").read_text(encoding="utf-8"))["strings"]
        assert "31" in strings["Ближайшие 31 день"]
    assert 'addItem("Ближайшие 31 день","month")' in (ROOT / "jarvis/ui/settings_dialog.py").read_text(encoding="utf-8")


def test_browser_launch_failure_is_reported(monkeypatch):
    import jarvis.integrations.google_calendar as calendar
    monkeypatch.setattr(calendar.webbrowser, "open", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        calendar.authorize_google_calendar("client-id.apps.googleusercontent.com", "", timeout_seconds=30)


def test_literal_names_are_plain_text():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from jarvis.ui.chat_view import ChatItem, MessageBubble
    QApplication.instance() or QApplication([])
    bubble = MessageBubble("assistant", "<b>QA</b>", "text", ChatItem("assistant", "<b>QA</b>", "text"))
    assert bubble.author_label.textFormat() == Qt.PlainText and bubble.author_label.text() == "<b>QA</b>"


def test_overlay_title_and_track_labels_are_plain_text():
    source = (ROOT / "jarvis/ui/overlay_manager.py").read_text(encoding="utf-8")
    assert "self.title_label.setTextFormat(Qt.PlainText)" in source
    assert "self.now_playing_label.setTextFormat(Qt.PlainText)" in source


def test_close_dialog_is_released_when_exec_fails(monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
    from jarvis.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])

    class Owner(QWidget):
        def _language_code(self):
            return "en"
    owner = Owner()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: (_ for _ in ()).throw(RuntimeError("synthetic")))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            MainWindow._ask_close_action(owner)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert owner.findChildren(QMessageBox) == []


def test_network_notice_survives_a_storage_error(tmp_path, monkeypatch):
    import sqlite3
    from jarvis.runtime import desktop_services
    service = desktop_services.DesktopServices.__new__(desktop_services.DesktopServices)
    service.config = type("C", (), {"get": lambda self, key, default=None: {"security.latency_threshold_ms": 250}.get(key, default)})()
    recorded = []
    fail = [True]

    class Store:
        def record(self, kind, payload, event_id=None, *, notify=False):
            if fail[0]:
                raise sqlite3.OperationalError("disk I/O error")
            recorded.append((kind, event_id))
            return True
    service.store = Store()
    service.callback = None
    service.network = type("N", (), {"observe": lambda self, *a: [{"type": "notification", "message_key": "network.latency_high", "data": {}}]})()
    with pytest.raises(sqlite3.Error):
        service._network_probe()
    assert service._network_pending186
    fail[0] = False
    service.network = type("N", (), {"observe": lambda self, *a: []})()
    service._network_probe()
    assert [kind for kind, _ in recorded] == ["network.latency_high"] and not service._network_pending186


def test_untouched_activity_switch_keeps_a_newer_external_value():
    source = (ROOT / "jarvis/ui/settings_dialog.py").read_text(encoding="utf-8")
    assert '("activity_enabled", "activity.enabled")' in source and "_toggle_snapshot186" in source


def test_foreground_error_resumes_queued_ambient_notices():
    source = (ROOT / "jarvis/ui/main_window.py").read_text(encoding="utf-8")
    block = source.split("def _handle_error(self, trace: str)", 1)[1].split("\n    def ", 1)[0]
    assert "self._ambient_typing_timer.start()" in block


def test_saved_settings_with_failed_autostart_is_a_partial_result(tmp_path, monkeypatch):
    from jarvis.config import ConfigManager
    for name in ("HOME", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(ConfigManager, "apply_wake_service", lambda *_: None)
    config = ConfigManager(ROOT / "config/default_settings.json")
    monkeypatch.setattr(ConfigManager, "apply_autostart", lambda *_: (_ for _ in ()).throw(PermissionError("Startup folder")))
    data = json.loads(json.dumps(config.data))
    data.setdefault("ui", {})["start_with_windows"] = not bool(config.get("ui.start_with_windows", False))
    config.save(data)                                          # no exception: the JSON is committed
    assert config.native_apply_error186 and "Startup folder" in config.native_apply_error186
    assert ConfigManager(ROOT / "config/default_settings.json").get("ui.start_with_windows") == config.get("ui.start_with_windows")


# ------------------------------------------------------------ earlier 186 UI fixes

def test_theme_state_icons_stay_readable_on_light_and_dark_fills(tmp_path):
    from jarvis.ui.theme_icons186 import contrast, icon_path, readable_on
    for background in ("#FFFFFF", "#F4F6FA", "#0A0F18", "#3B82F6", "#FACC15", "#10B981"):
        color = readable_on(background, "#DDE8F6")
        assert contrast(color, background) >= 3.0, background
        svg = Path(icon_path(tmp_path, "chevron_down", color)).read_text(encoding="utf-8")
        assert color.lower() in svg.lower()


def test_long_words_break_only_between_whole_clusters():
    from jarvis.ui.chat_view import _ZWSP, _break_token, _hinted_plain186
    flag = "🇷🇺"
    joined = _break_token("é" * 3 + flag * 2)
    assert joined.split(_ZWSP) == ["é"] * 3 + [flag] * 2
    assert _break_token("&WWWWW;").split(_ZWSP)[0] == "&"                 # plain text: entity is letters
    display, hints = _hinted_plain186("x" * 40)
    assert display.replace(_ZWSP, "") == "x" * 40 and hints
