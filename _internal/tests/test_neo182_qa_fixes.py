"""NEO 182: regressions for the NEO 181 QA report (files, settings, commands, dates, UI, hand)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---- files: LF-01 … LF-04, G04 ---------------------------------------------------------------

@pytest.fixture
def fs(tmp_path, monkeypatch):
    for key in ("HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.database import Database
    from jarvis.ollama_client import OllamaClient
    from jarvis.testing.atlas_session import AtlasConfig
    from jarvis.tools.registry import ToolRegistry
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = AtlasConfig(tmp_path / "profile")
    config.data["security"].update(full_user_file_access=False, restrict_file_mutations=True,
                                   allowed_roots=[str(allowed)], confirm_medium_risk=True)
    config.save()
    registry = ToolRegistry(config, Database(config.paths.database), OllamaClient(config))
    return SimpleNamespace(config=config, registry=registry, allowed=allowed, root=tmp_path)


@pytest.mark.parametrize("encoding", ["ascii", "bad-unknown-encoding"])
def test_a_failed_encoding_never_empties_the_file(fs, encoding):
    target = fs.allowed / "original.txt"
    target.write_bytes(b"ORIGINAL ASCII FILE")
    result = fs.registry.execute("filesystem", {"action": "write_text", "source": str(target), "content": "Привет", "encoding": encoding})
    assert not result.success
    assert target.read_bytes() == b"ORIGINAL ASCII FILE"
    assert not [p for p in fs.allowed.iterdir() if p.name != "original.txt" and p.suffix == ".neo-tmp"]


def test_move_out_of_a_folder_outside_the_allowed_roots_is_refused(fs):
    outside = fs.root / "excluded"
    outside.mkdir()
    private = outside / "private.txt"
    private.write_text("ORIGINAL")
    result = fs.registry.execute("filesystem", {"action": "move", "source": str(private), "destination": str(fs.allowed / "moved.txt")})
    assert not result.success
    assert private.read_text() == "ORIGINAL" and not (fs.allowed / "moved.txt").exists()


def test_two_text_pastes_in_the_same_second_keep_both(tmp_path, monkeypatch):
    from jarvis.runtime import clipboard168 as clip

    class Frozen:
        @staticmethod
        def now():
            from datetime import datetime
            return datetime(2026, 10, 2, 12, 0, 0)
    monkeypatch.setattr(clip, "datetime", Frozen)
    first = clip.save_to_folder(clip.Content("text", text="first"), tmp_path)
    second = clip.save_to_folder(clip.Content("text", text="second"), tmp_path)
    assert first[0] != second[0]
    assert first[0].read_text(encoding="utf-8") == "first" and second[0].read_text(encoding="utf-8") == "second"


@pytest.mark.parametrize("codec,bom", [("utf-16-le", b"\xff\xfe"), ("utf-8", b"\xef\xbb\xbf"), ("utf-8", b"")])
def test_paste_into_a_text_file_keeps_its_encoding(tmp_path, monkeypatch, codec, bom):
    from jarvis.runtime import clipboard168 as clip
    from jarvis.tools import copy_tool168
    target = tmp_path / "notes.txt"
    target.write_bytes(bom + "Исходный текст\n".encode(codec))
    monkeypatch.setattr(copy_tool168, "_content_or_error", lambda code: (clip.Content("text", text="Добавлено"), None))
    monkeypatch.setattr(copy_tool168, "_resolve", lambda config, name, kind: target)
    registry = SimpleNamespace(security=SimpleNamespace(assert_mutation_path_allowed=lambda p: p))
    config = SimpleNamespace(get=lambda key, default=None: "ru" if "language" in key else default)
    result = copy_tool168.paste_file(config, registry, "notes")
    assert result.success
    data = target.read_bytes()
    assert data.startswith(bom)
    assert data[len(bom):].decode(codec) == "Исходный текст\nДобавлено\n"


def test_a_file_in_an_unknown_encoding_is_not_touched(tmp_path, monkeypatch):
    from jarvis.runtime import clipboard168 as clip
    from jarvis.tools import copy_tool168
    target = tmp_path / "legacy.txt"
    target.write_bytes(b"\xcf\xf0\xe8\xe2\xe5\xf2 \xff\xfe\x00")
    before = target.read_bytes()
    monkeypatch.setattr(copy_tool168, "_content_or_error", lambda code: (clip.Content("text", text="x"), None))
    monkeypatch.setattr(copy_tool168, "_resolve", lambda config, name, kind: target)
    registry = SimpleNamespace(security=SimpleNamespace(assert_mutation_path_allowed=lambda p: p))
    result = copy_tool168.paste_file(SimpleNamespace(get=lambda key, default=None: default), registry, "legacy")
    assert not result.success and target.read_bytes() == before


@pytest.mark.parametrize("action", ["write_text", "append_text", "create_file", "create_folder", "rename", "copy", "move", "delete"])
def test_confirm_ordinary_file_changes_is_honoured(fs, action):
    from jarvis.agent import Agent
    args = {"action": action, "path": str(fs.allowed / "a.txt"), "source": str(fs.allowed / "a.txt"),
            "destination": str(fs.allowed / "b.txt"), "content": "x", "new_name": "c"}
    risk = fs.registry.risk_for("filesystem", args)
    proxy = SimpleNamespace(_confirmation_policy=None, config=fs.config, tools=fs.registry)
    assert Agent._needs_confirmation(proxy, "filesystem", args, risk) is True
    fs.config.data["security"]["confirm_medium_risk"] = False
    fs.config.data["security"]["auto_file_edits_without_confirmation"] = True
    assert Agent._needs_confirmation(proxy, "filesystem", args, risk) is False


def test_settings_migration_keeps_an_explicit_confirmation_choice():
    source = (ROOT / "jarvis" / "config.py").read_text(encoding="utf-8")
    assert 'security["confirm_medium_risk"] = False' not in source
    assert 'security.setdefault("confirm_medium_risk", False)' in source


# ---- secrets: SI-01, SI-02 ------------------------------------------------------------------

def test_api_keys_in_urls_never_reach_error_texts():
    from jarvis.runtime.diagnostics import safe_error
    text = safe_error(RuntimeError("400 for url: https://www.googleapis.com/youtube/v3/search?part=snippet&key=AIzaSECRET123&q=x"))
    assert "AIzaSECRET123" not in text and "part=snippet" in text
    assert "OWSECRET" not in safe_error("https://api.openweathermap.org/data/2.5/weather?q=Rome&appid=OWSECRET")


def test_spotify_drops_the_old_accounts_token_after_reconnect(monkeypatch):
    from jarvis.integrations.spotify import SpotifyAdapter
    store = {"integrations.spotify.refresh_token_encrypted": "ACCOUNT_A", "integrations.spotify.client_id_encrypted": "CID"}
    adapter = SpotifyAdapter.__new__(SpotifyAdapter)
    adapter.config = SimpleNamespace(get=lambda key, default=None: store.get(key, default))
    adapter._status_cache = {"track": "A"}
    adapter._status_cache_at = time.time()
    adapter._runtime_access_token = "TOKEN_A"
    adapter._runtime_expires_at = time.time() + 3000
    adapter._runtime_credentials = adapter._credentials_fingerprint()
    monkeypatch.setattr(SpotifyAdapter, "_refresh_if_needed", lambda self: "TOKEN_B")
    assert adapter._token() == "TOKEN_A"
    store["integrations.spotify.refresh_token_encrypted"] = "ACCOUNT_B"
    assert adapter._token() == "TOKEN_B"
    assert adapter._status_cache == {}


# ---- settings: SI-03 … SI-07 ----------------------------------------------------------------

def _config(tmp_path, monkeypatch):
    for key in ("HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.config import ConfigManager
    return ConfigManager(ROOT / "config" / "default_settings.json")


def test_concurrent_saves_do_not_share_a_temporary_file(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    import os as _os
    real_replace = _os.replace
    barrier = threading.Barrier(2, timeout=2)

    def slow_replace(src, dst):
        if str(dst).endswith("settings.json"):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real_replace(src, dst)
    monkeypatch.setattr("jarvis.config.os.replace", slow_replace)
    errors = []

    def writer(name):
        try:
            data = json.loads(json.dumps(config.data))
            data["user_name"] = name
            config.save(data)
        except Exception as exc:          # pragma: no cover - the defect
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(n,)) for n in ("WriterA", "WriterB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors
    saved = json.loads(config.paths.settings.read_text(encoding="utf-8"))
    assert saved["user_name"] in {"WriterA", "WriterB"}
    assert not list(config.paths.settings.parent.glob("settings*.tmp"))


@pytest.mark.parametrize("payload", ['{"app": ', "[]"])
def test_damaged_settings_are_kept_aside_and_neo_starts(tmp_path, monkeypatch, payload):
    config = _config(tmp_path, monkeypatch)
    settings = config.paths.settings
    settings.write_text(payload, encoding="utf-8")
    from jarvis.config import ConfigManager
    again = ConfigManager(ROOT / "config" / "default_settings.json")
    assert again.get("app.display_name") == "NEO"
    kept = list(again.paths.settings.parent.glob("settings.damaged-*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == payload
    assert again.recovered_settings_backup == str(kept[0])
    json.loads(settings.read_text(encoding="utf-8"))           # a valid file is back in place


@pytest.mark.parametrize("payload,field", [('{"app": null}', "app"), ('{"voice": {"silence_seconds": "oops"}}', "voice.silence_seconds"),
                                           ('{"settings_version": "broken"}', "settings_version")])
def test_one_wrong_field_is_repaired_and_the_rest_is_kept(tmp_path, monkeypatch, payload, field):
    """NEO 183: a single field of the wrong type goes back to its default; other settings survive."""
    config = _config(tmp_path, monkeypatch)
    settings = config.paths.settings
    data = json.loads(payload)
    data["user_name"] = "Kept"
    settings.write_text(json.dumps(data), encoding="utf-8")
    from jarvis.config import ConfigManager
    again = ConfigManager(ROOT / "config" / "default_settings.json")
    assert again.get("app.display_name") == "NEO"
    assert again.get("user_name") == "Kept"
    assert field in again.repaired_settings


def test_settings_apply_takes_spotify_tokens_from_the_live_settings():
    source = (ROOT / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    block = source[source.index('integrations.setdefault("spotify", {}).update({\n                "enabled": active_music_provider'):]
    block = block[:block.index("\"redirect_uri\"")]
    assert "_existing_spotify_refresh_token_encrypted" not in block
    assert 'self.config.get("integrations.spotify.refresh_token_encrypted"' in block


def test_late_spotify_oauth_result_after_cancel_changes_nothing():
    from jarvis.ui.settings_dialog import SettingsDialog
    saved = []
    dialog = SimpleNamespace(_settings_closed160=True, _spotify_auth_generation182=1,
                             _persist_media_partial=lambda *a, **k: saved.append(a))
    dialog._spotify_auth_stale182 = lambda g: SettingsDialog._spotify_auth_stale182(dialog, g)
    SettingsDialog._on_spotify_auth_success(dialog, {"access_token": "LATE", "refresh_token": "LATE"}, 1)
    assert saved == []
    dialog._settings_closed160 = False
    dialog._spotify_auth_generation182 = 2                     # a newer attempt started
    SettingsDialog._on_spotify_auth_success(dialog, {"access_token": "OLD"}, 1)
    assert saved == []


def test_uninstall_helper_removes_the_startup_launcher():
    for path in (ROOT / "installer_helper.ps1",):
        text = path.read_text(encoding="utf-8-sig")
        line = next(l for l in text.splitlines() if "priority_launch.vbs" in l and "foreach" in l)
        assert "'NEO.vbs'" in line and "'NEO_JARVIS.vbs'" in line


# ---- commands: CMD-01 … CMD-06, G01 … G03 ---------------------------------------------------

@pytest.mark.parametrize("code,text,payload", [
    ("fr", "écris n’ouvre pas l’application Calculator", "n’ouvre pas l’application Calculator"),
    ("es", "escribe no abras la aplicación Calculator", "no abras la aplicación Calculator"),
    ("it", "scrivi non aprire l’applicazione Calculator", "non aprire l’applicazione Calculator"),
    ("pt", "escreve não abras a aplicação Calculator", "não abras a aplicação Calculator"),
    ("ru", "напиши Привет, Мир!", "Привет, Мир!"),
])
def test_dictated_text_is_typed_as_said(code, text, payload):
    from jarvis.runtime.multilingual_commands import normalize_multilingual_command
    from jarvis.runtime.router import hybrid_router
    routed = hybrid_router().route(normalize_multilingual_command(text, code))
    assert routed is not None and routed.tool == "text_edit"
    assert routed.arguments == {"action": "dictate", "text": payload}


@pytest.mark.parametrize("code,text", [
    ("fr", "Mon ami a écrit : « ouvre l’application Calculator »"),
    ("pt", "O meu amigo escreveu: «abre a aplicação Calculator»"),
    ("en", 'My boss wrote: "delete the folder Reports"'),
])
def test_reported_speech_is_not_a_command(code, text):
    from jarvis.runtime.utterance_purpose import classify_utterance
    assert classify_utterance(text, code).purpose == "reported"


def test_two_actions_are_not_one_app_name():
    from jarvis.runtime.short_requests160 import complete
    assert complete("открой приложение Calculator затем открой приложение Notepad", "ru") is None


@pytest.mark.parametrize("code,text", [("en", "Could you please open application Calculator?"), ("fr", "ouvre l’application Calculator ?")])
def test_question_mark_is_not_part_of_the_app_name(code, text):
    from jarvis.runtime.canonical_commands import parse_canonical_command
    from jarvis.runtime.short_requests160 import complete
    from jarvis.runtime.router import hybrid_router
    from jarvis.runtime.multilingual_commands import normalize_multilingual_command
    names = []
    for result in (complete(text, code), hybrid_router().route(normalize_multilingual_command(text, code))):
        if result is None:
            continue
        args = result.arguments if hasattr(result, "arguments") else result[1]
        names.append(str(args.get("name", "")))
    assert names and all(n.strip() == "Calculator" or not n for n in names), names


def test_bare_french_pronoun_is_not_an_app_name():
    from jarvis.runtime.router import hybrid_router
    from jarvis.runtime.multilingual_commands import normalize_multilingual_command
    routed = hybrid_router().route(normalize_multilingual_command("ouvre ça", "fr"))
    assert routed is None or routed.arguments.get("name", "").casefold() not in {"ça", "ca", "cela"}


@pytest.mark.parametrize("code,text,expected", [
    ("pt", "que tempo faz", ("weather", {"action": "summary"})),
    ("es", "dónde están las partidas guardadas de Darktide", ("game_locations", {"action": "find", "name": "Darktide", "kind": "saves"})),
    ("en", "what is on the screen", ("screen", {"action": "analyze"})),
    ("fr", "qu’est-ce qu’il y a à l’écran", ("screen", {"action": "analyze"})),
])
def test_examples_from_the_getting_started_guides(code, text, expected):
    from jarvis.runtime.feature_commands import parse_feature_command
    assert parse_feature_command(text, code) == expected


def test_a_verb_is_never_a_weather_city():
    from jarvis.runtime.integration_phrases import extract_weather_location
    assert extract_weather_location("que погода faz")[0] == ""


# ---- dates and shelf: LF-05 … LF-09 ---------------------------------------------------------

class _Ru:
    def get(self, key, default=None):
        return "ru" if "language" in key else default


@pytest.fixture
def frozen_now(monkeypatch):
    from datetime import datetime as real
    import jarvis.tools.integrations as integrations

    class Now(real):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 2, 12, 0)
    monkeypatch.setattr(integrations, "datetime", Now)
    return Now


def test_an_impossible_date_is_refused(frozen_now):
    from jarvis.tools.integrations import _date_info
    result = _date_info(_Ru(), "31 февраля 2027")
    assert not result.success and result.data["outcome"] == "invalid_date"


def test_days_until_new_year_in_natural_russian(frozen_now):
    from jarvis.tools.integrations import _date_info
    result = _date_info(_Ru(), "сколько дней до Нового года")
    assert result.success and result.data["days"] == 91 and result.data["date"] == "2027-01-01"
    assert isinstance(result.text, str) and "91" in result.text


def test_days_between_two_dates(frozen_now):
    from jarvis.tools.integrations import _date_info
    result = _date_info(_Ru(), "сколько дней между 1 октября 2026 и 10 октября 2026")
    assert result.success and result.data["days"] == 9
    assert result.data["from"] == "2026-10-01" and result.data["to"] == "2026-10-10"


def test_a_written_date_is_read_as_written(frozen_now):
    from jarvis.tools.integrations import _date_info
    assert _date_info(_Ru(), "какой день недели 15.01.2027").data["date"] == "2027-01-15"
    assert _date_info(_Ru(), "29 февраля 2028").success


def test_shelf_create_and_list_respect_the_shelf(fs):
    created = fs.registry.execute("shelf", {"action": "create", "kind": "note", "title": "OTHER_SHELF", "content": "Keep", "shelf": "work", "order": 12})
    assert created.success
    fs.registry.execute("shelf", {"action": "create", "kind": "note", "title": "HOME", "content": "x"})
    rows = fs.registry.execute("shelf", {"action": "list", "shelf": "work"}).data
    assert [(r["title"], r["shelf"], r["order"]) for r in rows] == [("OTHER_SHELF", "work", 12)]
    assert fs.registry.execute("shelf", {"action": "list", "shelf": "nowhere"}).data == []


# ---- timers and status: NUI-01, NUI-02 ------------------------------------------------------

def test_unnamed_timer_gets_a_name_in_the_users_language(tmp_path):
    from jarvis.runtime.timers import TimerManager
    from jarvis.tools.integrations import _timer_label
    assert _timer_label("", "timer") == ""
    manager = TimerManager()
    manager.configure(tmp_path, "en")
    try:
        item = manager.create(5, "", "timer")
        assert item.label == "Timer · 5 sec"
    finally:
        manager.cancel()


@pytest.mark.parametrize("code,text,expected", [
    ("en", "Таймер: time is up.", "Timer: time is up."),
    ("fr", "Таймер : temps écoulé.", "Minuteur : temps écoulé."),
    ("pt", "Купить хлеб: o tempo acabou.", "Купить хлеб: o tempo acabou."),
    ("de", "Таймер: время вышло.", "Timer: Zeit ist abgelaufen."),
])
def test_time_is_up_survives_localisation(code, text, expected):
    from jarvis.runtime.localized_text import localize_runtime_text
    assert localize_runtime_text(text, code) == expected


@pytest.mark.parametrize("voice,wake,expect", [(True, True, "say «NEO»"), (True, False, "Press the microphone"), (False, False, "Microphone is off")])
def test_sleep_status_follows_the_listener(voice, wake, expect):
    from jarvis.ui.main_window import MainWindow
    values = {"voice.enabled": voice, "wake_word.enabled": wake, "wake_word.phrase": "NEO"}
    host = SimpleNamespace(config=SimpleNamespace(get=lambda key, default=None: values.get(key, default)),
                           _language_code=lambda: "en", _app_mic_muted=False)
    text = MainWindow._sleep_status182(host)
    assert expect in text
    assert "услышу" not in text


# ---- settings window and visuals: V01 … V09 -------------------------------------------------

def test_localised_tooltips_and_log_viewer(qapp_182, tmp_path):
    from jarvis.i18n import tr
    from jarvis.runtime.localized_text import localize_runtime_text
    from jarvis.ui.log_viewer import LogViewerWindow
    assert tr("Состояние NEO", "en") == "NEO status"
    assert localize_runtime_text("Показать логи", "en") == "Show logs"
    assert localize_runtime_text("Скрыть логи", "de") == "Protokolle ausblenden"
    viewer = LogViewerWindow(tmp_path / "missing.log", language="en")
    try:
        assert viewer._read_tail().decode("utf-8").startswith("The log has not been created yet:")
    finally:
        viewer.deleteLater()


@pytest.fixture(scope="module")
def qapp_182():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _settle(app, seconds=0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.003)


@pytest.fixture
def dialog(qapp_182, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from jarvis.config import ConfigManager
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.ui.qt_localization import install_ui_language
    from jarvis.ui.settings_dialog import SettingsDialog
    for key in ("HOME", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda self: None)
    config = ConfigManager(ROOT / "config" / "default_settings.json")
    config.data.setdefault("app", {})["language"] = "ru"
    config.data["language"] = "ru"
    install_ui_language(qapp_182, "ru")
    with external_io_blocked():
        d = SettingsDialog(config)
        d.resize(1000, 660)
        d.show()
        _settle(qapp_182)
        yield d
        d.reject()
        d.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _settle(qapp_182, 0.03)


def test_apply_keeps_the_current_section_title(dialog, qapp_182):
    dialog.tabs.setCurrentIndex(7)
    expected = dialog.tabs._buttons[7].text()
    dialog.apply_button.click()
    _settle(qapp_182)
    assert dialog.tabs._title.text() == expected


def test_search_uses_the_new_language_after_apply(dialog, qapp_182):
    tabs = dialog.tabs
    tabs._search.setText("Чувствительность")
    tabs._jump_to_first_hit()
    tabs._search.clear()
    _settle(qapp_182)
    tabs.setCurrentIndex(7)
    dialog.app_language.setCurrentIndex(dialog.app_language.findData("en"))
    dialog.apply_button.click()
    _settle(qapp_182)
    tabs._search.setText("Microphone sensitivity")
    tabs._jump_to_first_hit()
    _settle(qapp_182)
    assert not tabs._no_results.isVisible()


def test_search_hit_in_a_collapsed_integration_points_at_its_switch(dialog, qapp_182):
    tabs = dialog.tabs
    gmail_on = dialog.gmail_enabled.isChecked() if hasattr(dialog, "gmail_enabled") else None
    tabs._search.setText("Секрет клиента OAuth")
    tabs._jump_to_first_hit()
    _settle(qapp_182)
    assert tabs._hit_widget is not None and tabs._hit_widget.isVisible()
    assert getattr(tabs, "_collapsed_hint182", "")
    if gmail_on is not None:
        assert dialog.gmail_enabled.isChecked() == gmail_on       # the search never switches it on


def test_checkbox_shows_keyboard_focus(dialog, qapp_182):
    from PySide6.QtCore import Qt
    box = dialog.wake_enabled
    dialog.tabs._search.setFocus(Qt.TabFocusReason)
    _settle(qapp_182)
    before = box.grab().toImage()
    box.setFocus(Qt.TabFocusReason)
    _settle(qapp_182)
    assert box.hasFocus() and box.grab().toImage() != before


def test_wrapped_fields_have_an_accessible_name(dialog):
    from PySide6.QtGui import QAccessible
    interface = QAccessible.queryAccessibleInterface(dialog.sensitivity)
    assert interface.text(QAccessible.Text.Name)


def test_wrapped_form_labels_get_the_height_they_need(dialog, qapp_182):
    from PySide6.QtWidgets import QFormLayout, QLabel
    dialog.tabs.setCurrentIndex(9)
    _settle(qapp_182)
    page = dialog.tabs._stack.currentWidget()
    labels = []
    for form in page.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.LabelRole)
            if item and isinstance(item.widget(), QLabel) and item.widget().wordWrap() and item.widget().isVisible():
                labels.append(item.widget())
    assert labels
    for label in labels:
        assert label.height() >= label.heightForWidth(label.width()), label.text()


def test_close_dialog_text_is_readable_on_every_theme(qapp_182):
    source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "QMessageBox {{ background:" in source and "QMessageBox QLabel {{ color:{text_color}" in source
    assert '"dialog_bg":' in source


# ---- robot: the magnetic hand ----------------------------------------------------------------

def test_the_magnetic_hand_paints_in_both_poses(qapp_182):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QImage, QPainter
    from jarvis.ui.robot_widget import RobotWidget
    for opts in ({"coffee": True, "cigarette": False}, {"coffee": False, "cigarette": True}):
        robot = RobotWidget()
        robot.resize(520, 340)
        robot.set_background("#0C1017")
        robot.set_accessories(**opts)
        image = QImage(520, 340, QImage.Format_ARGB32_Premultiplied)
        for _ in range(30):
            robot._last_tick -= 1 / 60
            robot._advance_animation()
        image.fill(QColor("#0C1017"))
        painter = QPainter(image)
        robot.render(painter, QPoint())
        painter.end()
        assert not image.isNull()
    source = (ROOT / "jarvis" / "ui" / "robot_widget.py").read_text(encoding="utf-8")
    block = source[source.index("    def _draw_mag_hand181("):source.index("    def _draw_persistent_accessories(")]
    assert "wrist orb" in block and "prong" not in block
