"""NEO 183: user requests (robot, animations, chat, settings) and the NEO 182 QA report."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _settle(app, seconds=0.12):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.003)


def _robot(w=520, h=340):
    from jarvis.ui.robot_widget import RobotWidget
    robot = RobotWidget()
    robot.resize(w, h)
    robot.set_background("#0C1017")
    return robot


def _run(robot, seconds):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QImage, QPainter
    image = QImage(robot.width(), robot.height(), QImage.Format_ARGB32_Premultiplied)
    for _ in range(int(seconds * 60)):
        robot._last_tick -= 1 / 60
        robot._advance_animation()
        image.fill(QColor("#0C1017"))
        painter = QPainter(image)
        robot.render(painter, QPoint())
        painter.end()
    return image


# ---- robot ---------------------------------------------------------------------------------

def test_the_hand_holds_by_magnets_only():
    source = (ROOT / "jarvis" / "ui" / "robot_widget.py").read_text(encoding="utf-8")
    block = source[source.index("    def _draw_mag_hand181("):source.index("    def _draw_persistent_accessories(")]
    assert "petal" not in block and "prong" not in block          # no claws of any kind
    assert "magnet plate" in block and "gap" in block


def test_the_cigarette_is_smaller():
    source = (ROOT / "jarvis" / "ui" / "robot_widget.py").read_text(encoding="utf-8")
    assert "length, thick = 34.0 * u, 4.4 * u" in source


def test_the_cigarette_goes_to_the_side_opposite_the_mug_and_the_exhale_is_slow(qapp, monkeypatch):
    import jarvis.ui.robot_widget as rw
    monkeypatch.setattr(rw.random, "choice", lambda options: "cloud" if "cloud" in options else options[0])
    robot = _robot()
    robot.set_accessories(coffee=True, cigarette=True)
    robot._acc177["coffee"]["next"] = 10 ** 6
    robot._acc177["cigarette"]["next"] = 0.0
    robot._wisp_next181 = 10 ** 6
    _run(robot, 2.4)
    head_x, _head_y, _tilt = robot._head_frame177
    ember_x, _ember_y, _s = robot._ember177
    assert robot._cig_out181 > 0.9
    assert ember_x < head_x                           # left side; the mug floats on the right
    assert robot._exhale_until177 - robot._exhale_started181 >= 1.8
    smoke = [p for p in robot._particles177 if p[10] == 1.0 and p[2] < 0]
    assert smoke                                      # the smoke drifts left, towards the cigarette
    assert rw.RobotWidget._ACC_TIMING177["cigarette"][1] >= 2.5


# ---- animations ----------------------------------------------------------------------------

def test_stage_weather_runs_at_60_hz(qapp):
    robot = _robot()
    robot.show()
    _settle(qapp)
    robot.set_state("idle")
    robot.set_active_animation("off")
    assert robot._timer.interval() == 33
    robot.set_active_animation("rain")
    assert robot._timer.interval() == 16
    robot.hide()


def test_meteors_all_fall_the_same_way():
    source = (ROOT / "jarvis" / "ui" / "ambience180.py").read_text(encoding="utf-8")
    block = source[source.index("def _meteor("):source.index("def starfall(")]
    assert "math.pi - angle" not in block and "math.radians(208 + 10 * _h(seed))" in block


def test_clouds_are_solid_soft_silhouettes(qapp):
    from PySide6.QtGui import QColor
    from jarvis.ui import ambience180
    image = ambience180._cloud_image(QColor("#C8D6E8"), 1, 200, 84)
    centre = image.pixelColor(100, 52)
    assert centre.alpha() > 200                       # one opaque body, not stacked translucent discs
    row = [image.pixelColor(x, 52).alpha() for x in range(60, 140)]
    assert min(row) > 180                             # no holes where puffs overlap


def test_the_milky_way_stars_twinkle(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from jarvis.ui import ambience180

    def frame(t):
        image = QImage(520, 300, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        ambience180.starry_sky(painter, 520, 300, t, QColor("#DDE8FF"), False)
        painter.end()
        return image
    assert frame(1.0) != frame(2.7)
    source = (ROOT / "jarvis" / "ui" / "ambience180.py").read_text(encoding="utf-8")
    assert "the small stars inside the Milky Way light up and fade out" in source


# ---- main window, chat, settings -----------------------------------------------------------

def test_the_drag_strip_is_a_transparent_overlay():
    source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "outer.addWidget(self.header)" not in source
    assert "QFrame#dragHandle { background: transparent; border: none; }" in source
    assert "def _place_drag_handle183(self)" in source


@pytest.mark.parametrize("code,asked,answered", [
    ("ru", 'Telegram команда "какая погода"', 'Я ответил "Сейчас +5"'),
    ("en", 'Telegram command "какая погода"', 'I replied "Сейчас +5"'),
    ("pt", 'Comando do Telegram "какая погода"', 'Respondi "Сейчас +5"'),
])
def test_telegram_conversation_in_the_desktop_chat(code, asked, answered):
    from jarvis.integrations.telegram_remote import TELEGRAM_DIALOGUE_MARKER, telegram_dialogue_event
    config = SimpleNamespace(get=lambda key, default=None: code if "language" in key else default)
    event = telegram_dialogue_event(config, "какая погода", "Сейчас +5")
    assert event.startswith(TELEGRAM_DIALOGUE_MARKER)
    assert event[len(TELEGRAM_DIALOGUE_MARKER):] == asked + "\n" + answered


def test_user_name_is_empty_by_default_and_any_name_is_used_when_ticked():
    defaults = json.loads((ROOT / "config" / "default_settings.json").read_text(encoding="utf-8"))
    assert defaults["user_name"] == ""
    from jarvis.agent import Agent
    for name in ("Boss", "Алекс"):
        values = {"user_name": name, "user_name_enabled": True}
        agent = SimpleNamespace(config=SimpleNamespace(get=lambda key, default=None, v=values: v.get(key, default)))
        assert Agent._user_address_enabled(agent) is True
    values = {"user_name": "", "user_name_enabled": True}
    agent = SimpleNamespace(config=SimpleNamespace(get=lambda key, default=None: values.get(key, default)))
    assert Agent._user_address_enabled(agent) is False


@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_every_kind_of_address_has_several_phrasings(code):
    from jarvis.runtime.addressing import AddressContext, address_prefix
    for kind in ("action", "neutral", "gratitude", "farewell", "reassurance"):
        seen = {address_prefix(AddressContext(kind=kind), code, "Alex", counter) for counter in range(8)}
        assert len(seen) >= 4 and all("Alex" in item for item in seen), (kind, seen)


def test_nvidia_clips_live_on_the_games_page():
    source = (ROOT / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    games = source[source.index("    def _gaming_tab169(self)"):source.index("    def _games_tab(self)")]
    media = source[source.index("    def _media_tab(self)"):source.index("    def _media_tab(self)") + 6000]
    assert "self.capture_clip_hotkey" in games and "self.capture_clip_hotkey" not in media


def test_no_line_above_the_settings_buttons():
    source = (ROOT / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    assert "QWidget#settingsFooter {{ border:none;" in source


# ---- files, memory, dates (LF182) ----------------------------------------------------------

@pytest.fixture
def fs(tmp_path, monkeypatch):
    for key in ("HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.database import Database
    from jarvis.ollama_client import OllamaClient
    from jarvis.testing.atlas_session import AtlasConfig
    from jarvis.tools.registry import ToolRegistry
    allowed = tmp_path / "allowed"
    excluded = tmp_path / "excluded"
    allowed.mkdir()
    excluded.mkdir()
    config = AtlasConfig(tmp_path / "profile")
    config.data["security"].update(full_user_file_access=False, restrict_file_mutations=True,
                                   allowed_roots=[str(allowed)], confirm_medium_risk=False)
    config.save()
    database = Database(config.paths.database)
    registry = ToolRegistry(config, database, OllamaClient(config))
    return SimpleNamespace(config=config, registry=registry, allowed=allowed, excluded=excluded, db=database)


def _fs(fs, **args):
    return fs.registry.execute("filesystem", args)


def test_copy_never_writes_through_a_link_to_a_forbidden_file(fs):
    (fs.allowed / "src").mkdir()
    (fs.allowed / "src" / "protected.txt").write_text("NEW")
    private = fs.excluded / "protected.txt"
    private.write_text("PRIVATE")
    (fs.allowed / "dst").mkdir()
    os.symlink(private, fs.allowed / "dst" / "protected.txt")
    result = _fs(fs, action="copy", source=str(fs.allowed / "src" / "protected.txt"), destination=str(fs.allowed / "dst"))
    assert not result.success and private.read_text() == "PRIVATE"


def test_undo_respects_the_current_policy_and_never_follows_a_new_link(fs):
    target = fs.allowed / "file.txt"
    target.write_text("OLD")
    assert _fs(fs, action="write_text", source=str(target), content="NEW").success
    victim = fs.excluded / "victim.txt"
    victim.write_text("VICTIM")
    target.unlink()
    os.symlink(victim, target)
    assert not _fs(fs, action="undo_last_change").success
    assert victim.read_text() == "VICTIM"
    # revoked folder
    target.unlink()
    target.write_text("NEW")
    other = fs.allowed / "other.txt"
    other.write_text("A")
    assert _fs(fs, action="write_text", source=str(other), content="B").success
    fs.config.data["security"]["allowed_roots"] = [str(fs.excluded)]
    assert not _fs(fs, action="undo_last_change").success
    assert other.read_text() == "B"


@pytest.mark.parametrize("codec,bom,newline", [("cp1251", b"", "\n"), ("utf-16-le", b"\xff\xfe", "\r\n"), ("utf-8", b"", "\r\n")])
def test_append_keeps_the_files_encoding_and_line_breaks(fs, codec, bom, newline):
    target = fs.allowed / "old.txt"
    target.write_bytes(bom + f"Старый текст{newline}".encode(codec))
    result = _fs(fs, action="append_text", source=str(target), content="NEW\n")
    assert result.success, result.text
    assert target.read_bytes() == bom + f"Старый текст{newline}NEW{newline}".encode(codec)


def test_append_refuses_binary_and_unknown_files(fs):
    binary = fs.allowed / "binary.dat"
    binary.write_bytes(b"\x00\x01\x02\x03")
    result = _fs(fs, action="append_text", source=str(binary), content="text")
    assert not result.success and result.data["outcome"] == "binary_file"
    assert binary.read_bytes() == b"\x00\x01\x02\x03"
    assert "unknown text encoding" not in result.text and "internal_bug" not in str(result.data)


def test_crlf_write_succeeds_and_is_undone_first(fs):
    previous = fs.allowed / "previous.txt"
    previous.write_text("P0")
    assert _fs(fs, action="write_text", source=str(previous), content="P1").success
    crlf = fs.allowed / "crlf.txt"
    crlf.write_text("C0")
    result = _fs(fs, action="write_text", source=str(crlf), content="NEW\r\nTEXT")
    assert result.success, result.text
    assert crlf.read_bytes() == b"NEW\r\nTEXT"
    assert _fs(fs, action="undo_last_change").success
    assert crlf.read_text() == "C0" and previous.read_text() == "P1"


def test_zip_into_itself_is_refused(fs):
    import zipfile
    archive = fs.allowed / "original.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("document.txt", "content")
    before = archive.read_bytes()
    result = _fs(fs, action="zip", source=str(archive), destination=str(archive))
    assert not result.success and archive.read_bytes() == before


def test_paste_refuses_binary_and_keeps_crlf(tmp_path, monkeypatch):
    from jarvis.runtime import clipboard168 as clip
    from jarvis.tools import copy_tool168
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"\x00\x01\x02\x03")
    registry = SimpleNamespace(security=SimpleNamespace(assert_mutation_path_allowed=lambda p: p))
    config = SimpleNamespace(get=lambda key, default=None: "en" if "language" in key else default)
    monkeypatch.setattr(copy_tool168, "_content_or_error", lambda code: (clip.Content("text", text="Added 🌍"), None))
    monkeypatch.setattr(copy_tool168, "_resolve", lambda c, name, kind: binary)
    result = copy_tool168.paste_file(config, registry, "binary")
    assert not result.success and binary.read_bytes() == b"\x00\x01\x02\x03"
    text = tmp_path / "windows.txt"
    text.write_bytes(b"one\r\ntwo\r\n")
    monkeypatch.setattr(copy_tool168, "_resolve", lambda c, name, kind: text)
    assert copy_tool168.paste_file(config, registry, "windows").success
    assert text.read_bytes() == "one\r\ntwo\r\nAdded 🌍\r\n".encode("utf-8")


def test_memory_clear_removes_everything_and_limits_are_told(fs):
    from jarvis.runtime import memory169 as memory
    config = SimpleNamespace(get=lambda key, default=None: True if key.endswith("enabled") else default, paths=SimpleNamespace(root=fs.allowed))
    for i in range(510):
        fs.db.set_memory(f"pref:k{i}", json.dumps({"name": f"k{i}", "value": "v"}))
    memory.clear(fs.db, config, "en")
    assert not memory._user_rows(fs.db, limit=100000)
    ok, text = memory.note(fs.db, config, "A" * 1000 + " IMPORTANT TAIL", "en")
    assert not ok and "1000" in text
    ok, _ = memory.remember(fs.db, config, "B", "C" * 500 + " TAIL", "en")
    assert not ok
    ok, _ = memory.remember(fs.db, config, "A" * 120 + " first", "FIRST", "en")
    assert not ok                                     # no silent cut that would overwrite another key


class _Lang:
    def __init__(self, code):
        self.code = code

    def get(self, key, default=None):
        return self.code if "language" in key else default


@pytest.fixture
def today(monkeypatch):
    from datetime import datetime as real
    import jarvis.tools.integrations as integrations

    def at(y, m, d):
        class Now(real):
            @classmethod
            def now(cls, tz=None):
                return cls(y, m, d, 12, 0)
        monkeypatch.setattr(integrations, "datetime", Now)
    at(2026, 10, 2)
    return at


@pytest.mark.parametrize("text", ["2027-02-31", "31 фев 2027", "какой праздник 31 февраля 2027"])
def test_impossible_dates_in_every_form_are_refused(today, text):
    from jarvis.tools.integrations import _date_info
    result = _date_info(_Lang("ru"), text)
    assert not result.success and result.data["outcome"] == "invalid_date"


def test_date_arithmetic(today):
    from jarvis.tools.integrations import _date_info
    iso = _date_info(_Lang("ru"), "сколько дней между 2026-10-01 и 2026-10-10")
    assert iso.data["days"] == 9
    until = _date_info(_Lang("ru"), "сколько дней до 10 октября 2026")
    assert until.data["days"] == 8 and "8" in until.text
    since = _date_info(_Lang("ru"), "сколько дней до 1 октября 2026")
    assert since.data["days"] == -1
    assert _date_info(_Lang("en"), "12/31/2026").data["date"] == "2026-12-31"
    assert _date_info(_Lang("ru"), "12/31/2026").data["date"] == "2026-12-31"   # unambiguous either way
    assert _date_info(_Lang("en"), "10/11/2026").data["date"] == "2026-10-11"
    assert _date_info(_Lang("ru"), "10.11.2026").data["date"] == "2026-11-10"
    today(2027, 1, 1)
    new_year = _date_info(_Lang("ru"), "сколько дней до Нового года")
    assert new_year.data == {"date": "2027-01-01", "days": 0}


def test_shelf_read_looks_inside_the_chosen_shelf(fs):
    shelf = lambda **a: fs.registry.execute("shelf", a)
    assert shelf(action="create", kind="note", title="Scoped title", content="WORK", shelf="work").success
    assert shelf(action="create", kind="note", title="Scoped title", content="HOME", shelf="personal").success
    ambiguous = shelf(action="read", name="Scoped title")
    assert not ambiguous.success and {m["shelf"] for m in ambiguous.data["matches"]} == {"work", "personal"}
    scoped = shelf(action="read", name="Scoped title", shelf="work")
    assert scoped.success and scoped.text == "WORK"


# ---- settings (N182-SI) --------------------------------------------------------------------

def _config(tmp_path, monkeypatch):
    for key in ("HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.config import ConfigManager
    return ConfigManager(ROOT / "config" / "default_settings.json")


@pytest.mark.parametrize("roots", [None, 42, True])
def test_broken_allowed_roots_never_block_the_start(tmp_path, monkeypatch, roots):
    config = _config(tmp_path, monkeypatch)
    config.paths.settings.write_text(json.dumps({"security": {"allowed_roots": roots}, "user_name": "Kept"}), encoding="utf-8")
    from jarvis.config import ConfigManager
    again = ConfigManager(ROOT / "config" / "default_settings.json")
    assert isinstance(again.get("security.allowed_roots"), list)
    assert again.get("user_name") == "Kept"


def _spotify(config, session):
    from jarvis.integrations.spotify import SpotifyAdapter
    adapter = SpotifyAdapter.__new__(SpotifyAdapter)
    adapter.config = config
    adapter.session = session
    adapter._secret_cache = {}
    adapter._status_cache = {}
    adapter._status_cache_at = 0.0
    return adapter


def _set_spotify(config, refresh, client, access="", expires=0.0):
    from jarvis.secrets import protect_secret
    data = json.loads(json.dumps(config.data))
    node = data.setdefault("integrations", {}).setdefault("spotify", {})
    node.update(refresh_token_encrypted=protect_secret(refresh), client_id_encrypted=protect_secret(client),
                access_token_encrypted=protect_secret(access), token_expires_at=expires)
    config.save(data, credentials=True)


def test_a_late_refresh_never_overwrites_a_new_connection(tmp_path, monkeypatch):
    from jarvis.integrations.spotify import SpotifyAPIError
    from jarvis.secrets import unprotect_secret
    config = _config(tmp_path, monkeypatch)
    _set_spotify(config, "FAKE_REFRESH_A", "FAKE_CLIENT_A")

    class Session:
        def post(self, *_a, **_k):
            _set_spotify(config, "FAKE_REFRESH_B", "FAKE_CLIENT_B")          # reconnected meanwhile
            return SimpleNamespace(status_code=200, content=b"1",
                                   json=lambda: {"access_token": "FAKE_A_LATE_ACCESS", "refresh_token": "FAKE_A_LATE_REFRESH", "expires_in": 3600})
    adapter = _spotify(config, Session())
    with pytest.raises(SpotifyAPIError):
        adapter._refresh_if_needed(force=True)
    disk = json.loads(config.paths.settings.read_text(encoding="utf-8"))["integrations"]["spotify"]
    assert unprotect_secret(disk["refresh_token_encrypted"]) == "FAKE_REFRESH_B"
    assert getattr(adapter, "_runtime_access_token", "") != "FAKE_A_LATE_ACCESS"


def test_apply_keeps_a_token_rotated_in_between(tmp_path, monkeypatch):
    from jarvis.secrets import protect_secret, unprotect_secret
    config = _config(tmp_path, monkeypatch)
    _set_spotify(config, "FAKE_REFRESH_A", "FAKE_CLIENT_A")
    snapshot = json.loads(json.dumps(config.data))                 # Apply prepares its data…
    config.update(lambda d: d["integrations"]["spotify"].update(refresh_token_encrypted=protect_secret("FAKE_ROTATED")), credentials=True)
    snapshot["user_name"] = "Changed in Settings"
    config.save(snapshot)                                          # …and writes it after the refresh
    disk = json.loads(config.paths.settings.read_text(encoding="utf-8"))
    assert unprotect_secret(disk["integrations"]["spotify"]["refresh_token_encrypted"]) == "FAKE_ROTATED"
    assert disk["user_name"] == "Changed in Settings"


def test_spotify_sign_in_saves_the_client_that_was_asked():
    from jarvis.secrets import unprotect_secret
    from jarvis.ui.settings_dialog import SettingsDialog
    saved = []
    field = SimpleNamespace(value="FAKE_CLIENT_B")
    field.text = lambda: field.value
    field.setText = lambda v: setattr(field, "value", v)
    dialog = SimpleNamespace(
        _settings_closed160=False, _spotify_auth_generation182=1,
        _spotify_auth_context183={"client_id": "FAKE_CLIENT_A", "redirect_uri": "http://127.0.0.1:8765/callback"},
        spotify_client_id=field, spotify_redirect_uri=SimpleNamespace(text=lambda: "http://other/"),
        spotify_integration_enabled=SimpleNamespace(setChecked=lambda v: None),
        spotify_connect_button=SimpleNamespace(setText=lambda v: None),
        spotify_status=None, _status_text=lambda *a, **k: None, _t=lambda v: v,
        _persist_media_partial=lambda provider, values, **k: saved.append(values),
    )
    dialog._spotify_auth_stale182 = lambda g: SettingsDialog._spotify_auth_stale182(dialog, g)
    SettingsDialog._on_spotify_auth_success(dialog, {"access_token": "A", "refresh_token": "R"}, 1)
    assert saved and unprotect_secret(saved[0]["client_id_encrypted"]) == "FAKE_CLIENT_A"
    assert saved[0]["redirect_uri"] == "http://127.0.0.1:8765/callback"


def test_yandex_sign_in_after_cancel_is_ignored():
    from jarvis.ui.settings_dialog import SettingsDialog
    dialog = SimpleNamespace(_settings_closed160=True, _yandex_auth_generation183=1)
    assert SettingsDialog._yandex_auth_stale183(dialog, 1) is True
    dialog._settings_closed160 = False
    assert SettingsDialog._yandex_auth_stale183(dialog, 1) is False
    dialog._yandex_auth_generation183 = 2
    assert SettingsDialog._yandex_auth_stale183(dialog, 1) is True
    source = (ROOT / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    assert "worker.signals.result.connect(lambda result, g=g: None if self._yandex_auth_stale183(g)" in source


def test_youtube_check_never_shows_the_key():
    source = (ROOT / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    block = source[source.index("    def _test_youtube_api(self)"):source.index("    def _adopt_scroll_sections")]
    # NEO 187 REC-PRIVACY R1: the key is removed BEFORE logging/shortening
    # (a cut could leave part of it); replacing afterwards was too late.
    assert '_short_network_error184(trace, secrets=(key,))' in block
    from jarvis.runtime.diagnostics import safe_error
    error = RuntimeError("ConnectionError: https://www.googleapis.com/youtube/v3/search?q=YouTube&key=FAKE_YOUTUBE_UI_KEY_123456789")
    assert "FAKE_YOUTUBE_UI_KEY_123456789" not in safe_error(error)


# ---- commands (CG182) ----------------------------------------------------------------------

@pytest.mark.parametrize("code,text,expect", [
    ("es", 'escribe "Hola"', "unknown"), ("pt", 'escreve "Olá"', "unknown"),
    ("en", 'type My boss wrote: "Hello"', "unknown"), ("en", 'copy to clipboard My boss wrote: "Hello"', "unknown"),
    ("ru", "На экране написано: «открой приложение Calculator»", "reported"),
    ("ru", "Начальник написал: открой приложение Calculator", "reported"),
    ("pt", "Na mensagem está escrito: «abre a aplicação Calculator»", "reported"),
    ("fr", "Mon ami a écrit : « ouvre l’application Calculator »", "reported"),
])
def test_reported_speech_and_explicit_dictation(code, text, expect):
    from jarvis.runtime.utterance_purpose import classify_utterance
    assert classify_utterance(text, code).purpose == expect


def test_text_slots_keep_their_punctuation():
    from jarvis.runtime import command_contracts150
    assert command_contracts150.complete("copy to clipboard Hello, World!", "en") == ("clipboard", {"action": "set", "text": "Hello, World!"})
    from jarvis.agent import _restore_payload183
    routed = SimpleNamespace(arguments={"action": "dictate", "text": "hello, world"})
    _restore_payload183(routed, "escribe Hello, World!")
    assert routed.arguments["text"] == "Hello, World!"


# ---- interface (V182) ----------------------------------------------------------------------

def test_log_viewer_theme_language_and_scroll(qapp, tmp_path):
    from jarvis.ui.log_viewer import LogViewerWindow
    log = tmp_path / "neo.log"
    log.write_text("\n".join(f"line {i}" for i in range(1000)) + "\n", encoding="utf-8")
    viewer = LogViewerWindow(log, language="ru")
    viewer.apply_theme("#EAF2FF", "#0C1017")
    assert "background:#0C1017" in viewer.styleSheet() and "color:#EAF2FF" in viewer.styleSheet()
    viewer.set_language("en")
    assert viewer.windowTitle() != "NEO — логи"
    viewer.resize(600, 400)
    viewer.show()
    _settle(qapp)
    bar = viewer.output.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    _settle(qapp)
    middle = viewer.output.firstVisibleBlock().blockNumber()
    assert middle > 100
    with log.open("a", encoding="utf-8") as handle:
        handle.write("new line\n")
    viewer.refresh()
    _settle(qapp)
    assert abs(viewer.output.firstVisibleBlock().blockNumber() - middle) < 30
    viewer.hide()
    viewer.deleteLater()


def test_chat_shows_user_text_literally_and_wraps_long_words(qapp):
    from PySide6.QtCore import Qt
    from jarvis.ui.chat_view import ChatView
    chat = ChatView()
    chat.resize(300, 400)
    user = chat.add_user("BEGIN <b>tag</b> &amp;\nEND")
    assert user.body_label.textFormat() == Qt.PlainText
    assert user.text == "BEGIN <b>tag</b> &amp;\nEND"
    long = chat.add_assistant("BEGIN_" + "x" * 1000 + "_END")
    assert "​" in long.body_label.text() and long.text.endswith("_END")
    chat.deleteLater()


def test_accessible_names_follow_the_language_and_reach_inner_editors(qapp, tmp_path, monkeypatch):
    from PySide6.QtGui import QAccessible
    from jarvis.config import ConfigManager
    from jarvis.i18n import translate_widget_tree
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.ui.qt_localization import install_ui_language
    from jarvis.ui.settings_dialog import SettingsDialog
    for key in ("HOME", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda self: None)
    config = ConfigManager(ROOT / "config" / "default_settings.json")
    config.data.setdefault("app", {})["language"] = "en"
    config.data["language"] = "en"
    install_ui_language(qapp, "en")
    with external_io_blocked():
        dialog = SettingsDialog(config)
        dialog.show()
        _settle(qapp, 0.3)
        translate_widget_tree(dialog, "en")
        name = QAccessible.queryAccessibleInterface(dialog.sensitivity).text(QAccessible.Text.Name)
        assert name and not any("Ѐ" <= c <= "ӿ" for c in name)
        assert QAccessible.queryAccessibleInterface(dialog.user_name).text(QAccessible.Text.Name)
        dialog._apply_microphone_rows([], "synthetic:7") if dialog._apply_microphone_rows.__code__.co_argcount >= 3 else None
        texts = [dialog.input_device.itemText(i) for i in range(dialog.input_device.count())]
        assert not any("Системный микрофон" in t or "Текущий микрофон" in t for t in texts), texts
        dialog.reject()
        dialog.deleteLater()
