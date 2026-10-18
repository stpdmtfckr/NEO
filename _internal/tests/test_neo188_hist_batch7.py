"""NEO 188: historical records HIST-045 … (app choice vs dictionary, autoclicker ownership, audio jobs…)."""
import threading
import time
from types import SimpleNamespace

import pytest


def test_hist045_confirmed_menu_choice_is_not_replaced_by_the_dictionary(tmp_path, monkeypatch):
    from jarvis.desktop_resolver import ResolvedTarget
    from jarvis.testing.atlas_session import AtlasConfig, AuditAgent
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.tools import implementations
    a_exe, b_exe = tmp_path / "alpha.exe", tmp_path / "beta.exe"
    a_exe.write_bytes(b"MZ"); b_exe.write_bytes(b"MZ")
    launches = []
    with external_io_blocked():
        agent = AuditAgent(AtlasConfig(tmp_path / "profile"))
        try:
            agent.config.data.setdefault("personal_dictionary", {})["entries"] = [
                {"kind": "app", "name": "Orbit", "path": str(b_exe)}]
            monkeypatch.setattr(implementations.subprocess, "Popen", lambda args, **k: launches.append(list(args)))
            monkeypatch.setattr(implementations.os, "startfile", lambda path: launches.append([str(path)]), raising=False)
            monkeypatch.setattr(implementations, "application_candidates", lambda q, limit=5: [])
            token = implementations.SELECTED_APP_CANDIDATE185.set({"kind": "app", "value": str(a_exe), "label": "Orbit", "score": 1.0})
            try:
                agent.tools._tools["application"].handler({"action": "open", "name": "Orbit", "kind": "app"})
            except Exception:
                pass                                                # verification after the launch is out of scope
            finally:
                implementations.SELECTED_APP_CANDIDATE185.reset(token)
            assert launches and launches[0][0] == str(a_exe)        # the chosen A, never the dictionary B
            launches.clear()
            agent.tools._tools["application"].handler({"action": "open", "name": "Orbit", "kind": "app"})
            assert launches and launches[0][0] == str(b_exe)        # without a menu choice the dictionary applies
        finally:
            agent.database.close()


def _clicker(monkeypatch, click):
    from jarvis.runtime.autoclicker import AutoClickerController
    config = SimpleNamespace(get=lambda key, default=None: {"autoclicker.enabled": True, "autoclicker.cps": 50.0}.get(key, default))
    controller = AutoClickerController(config)
    monkeypatch.setattr(controller, "_click", click)
    return controller


def test_hist049_old_run_tail_cannot_touch_the_new_run(monkeypatch):
    clicks = []
    controller = _clicker(monkeypatch, lambda button: clicks.append(threading.current_thread().name))
    controller.start()
    old_run, old_thread = controller._owner, controller._thread
    controller.stop()
    controller.start()                                       # B starts before A's tail ran
    new_run = controller._owner
    assert new_run is not old_run and not new_run.stop.is_set() and old_run.stop.is_set()
    old_thread.join(1)
    assert controller.status()["active"] is True and controller._owner is new_run   # A's tail reset nothing
    time.sleep(0.1)
    assert clicks                                            # B keeps clicking (non-zero drain)
    controller._run(old_run)                                 # A continues by hand: never re-admitted
    assert controller.status()["active"] is True and controller._owner is new_run
    controller.stop()
    controller._thread.join(1)


def test_hist050_backend_failure_ends_the_run_and_is_reported(monkeypatch):
    def broken(button):
        raise OSError("input backend failed")
    controller = _clicker(monkeypatch, broken)
    controller.start()
    controller._thread.join(1)
    status = controller.status()
    assert status["active"] is False and "input backend failed" in status["error"]
    monkeypatch.setattr(controller, "_click", lambda button: None)
    controller.start()                                       # a new start recovers and clears the old error
    assert controller.status()["active"] is True and "error" not in controller.status()
    controller.stop()
    controller._thread.join(1)


def test_hist054_timeout_distinguishes_not_started_from_unknown_outcome():
    from jarvis.audio.control_worker import AudioControlExecutor, AudioJobOutcomeUnknown
    executor = AudioControlExecutor()
    gate, ran = threading.Event(), []
    try:
        blocker = threading.Thread(target=lambda: executor.submit(lambda: gate.wait(2), timeout=3))
        blocker.start()
        time.sleep(0.05)
        with pytest.raises(TimeoutError) as queued:          # waits behind the blocker, never starts
            executor.submit(lambda: ran.append("queued"), timeout=0.2)
        assert not isinstance(queued.value, AudioJobOutcomeUnknown)
        gate.set(); blocker.join(2)
        time.sleep(0.05)
        assert ran == []                                      # an abandoned queued job is never executed later
        slow = threading.Event()
        with pytest.raises(AudioJobOutcomeUnknown):           # it had started: the outcome is unknown
            executor.submit(lambda: slow.wait(0.6), timeout=0.2)
    finally:
        executor.close(timeout=1.0)


class _Endpoint:
    def __init__(self, level):
        self.level = level

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, value, context):
        self.level = value


def test_hist055_relative_change_reads_writes_and_reads_back_one_device(monkeypatch):
    from jarvis.audio.providers.windows_core_audio import WindowsCoreAudioOutputProvider as provider
    speakers, headset = _Endpoint(0.40), _Endpoint(0.90)
    state = {"default": "speakers"}
    devices = {"speakers": speakers, "headset": headset}

    def endpoint(device_id=None):
        chosen = device_id or state["default"]
        state["default"] = "headset"                          # the user switches the default meanwhile
        return devices[chosen]
    monkeypatch.setattr(provider, "_raw_device", classmethod(lambda cls, device_id=None: device_id or state["default"]))
    monkeypatch.setattr(provider, "_device_id", classmethod(lambda cls, dev: str(dev)))
    monkeypatch.setattr(provider, "_endpoint_volume", classmethod(lambda cls, device_id=None: endpoint(device_id)))
    result = provider.change_volume(10)
    assert result == {"before": 40, "value": 50, "device_id": "speakers"}
    assert abs(speakers.level - 0.50) < 1e-9 and abs(headset.level - 0.90) < 1e-9   # the headset was not touched
    state["default"] = "speakers"
    assert provider.set_volume(30) == 30 and abs(headset.level - 0.90) < 1e-9       # readback on the same endpoint


def test_hist056_powershell_switch_is_verified_only_by_the_read_back_id(monkeypatch):
    import json as _json
    from jarvis.audio.output_controller import AudioOutputController as controller
    answers = {}
    monkeypatch.setattr(controller, "_run", staticmethod(lambda script, timeout=10.0: SimpleNamespace(returncode=0, stdout=answers["out"], stderr="")))
    monkeypatch.setattr(controller, "_ensure", classmethod(lambda cls, proc: proc.stdout))
    answers["out"] = _json.dumps({"ID": "A", "Name": "Speakers", "CurrentID": "A", "CurrentName": "Speakers"})
    assert controller._powershell_set_default(device_id="A")["verified"] is True
    answers["out"] = _json.dumps({"ID": "A", "Name": "Speakers"})                     # no readback at all
    missing = controller._powershell_set_default(device_id="A")
    assert missing["verified"] is False and missing["readback"] == "missing" and missing["is_default"] is False
    answers["out"] = _json.dumps({"ID": "A", "Name": "Speakers", "CurrentID": "B", "CurrentName": "Speakers"})
    twin = controller._powershell_set_default(device_name="Speakers")                 # same name, other device
    assert twin["verified"] is False and twin["readback"] == "mismatch"
    answers["out"] = "not json"
    assert controller._powershell_set_default(device_id="A")["verified"] is False


def test_hist057_telegram_device_button_resolves_from_its_own_menu_snapshot():
    from jarvis.integrations.telegram_remote import TelegramRemoteController
    remote = TelegramRemoteController.__new__(TelegramRemoteController)
    catalog = [{"type": "output", "device_id": "A", "name": "Speakers"}, {"type": "output", "device_id": "B", "name": "Headset"}]
    remote._audio_devices = lambda chat_id, force=False: [dict(x) for x in catalog]
    remote._panel = lambda *a, **k: None
    remote._show_audio_device_menu("7", 1, device_type="output")
    revision = remote._audio_device_menu_rev188
    catalog.reverse()                                          # the list is re-ordered before the press
    assert remote._audio_menu_choice188("7", "o", 0, revision) == ("A", "Speakers")   # still the shown A
    assert remote._audio_menu_choice188("7", "o", 0, revision - 1) is None            # an older menu is stale
    catalog[:] = [x for x in catalog if x["device_id"] != "A"]
    assert remote._audio_menu_choice188("7", "o", 0, revision) is None                # A vanished: ask again


def test_hist058_restore_level_belongs_to_the_same_device(monkeypatch):
    from jarvis.audio.input_controller import AudioInputController
    controller = AudioInputController()
    levels = {"A": 60, "B": 80}
    muted = {}
    state = {"default": "B"}

    class Native:
        name = "native"

        @staticmethod
        def available():
            return True

        @staticmethod
        def default_device():
            return SimpleNamespace(device_id=state["default"])

        @staticmethod
        def get_volume(device_id=None):
            return levels[device_id or state["default"]]

        @staticmethod
        def set_mute(value, device_id=None):
            muted[device_id or state["default"]] = value
            return value

        @staticmethod
        def set_volume(value, device_id=None):
            levels[device_id or state["default"]] = value
            return value
    controller.native = Native
    monkeypatch.setattr(controller, "_call", lambda fn, *a, timeout=8.0, **k: fn(*a, **k))
    controller.set_mute(True)                                   # default (B, 80) muted
    assert controller._pre_mute_volume == {"B": 80}
    levels["A"] = 0
    result = controller.set_mute(False, device_id="A")         # unmute an explicit A
    assert "restored_volume" not in result and levels["A"] == 0   # B's 80 is never applied to A
    levels["A"] = 55
    controller.set_mute(True, device_id="A")                    # A's own level is saved under A
    assert controller._pre_mute_volume["A"] == 55
    levels["A"] = 0                                             # a driver that mutes by zeroing the level
    assert controller.set_mute(False, device_id="A")["restored_volume"] == 55 and levels["A"] == 55


def test_hist059_late_device_list_never_overwrites_a_newer_pick():
    from PySide6.QtWidgets import QApplication, QComboBox
    QApplication.instance() or QApplication([])
    from jarvis.ui.settings_dialog import SettingsDialog
    combo = QComboBox()
    for label, token in (("Default", ""), ("Mic A", "A"), ("Mic B", "B")):
        combo.addItem(label, token)
    dialog = SimpleNamespace(_settings_closed160=False, input_device=combo, _mic_draft_rev188=0,
                             _t=lambda text: text, _add_current_device186=lambda device: combo.addItem(device, device))
    combo.setCurrentIndex(1)                                     # A was selected when the refresh started
    revision = dialog._mic_draft_rev188
    combo.setCurrentIndex(2); dialog._mic_draft_rev188 += 1      # the user picks B meanwhile
    SettingsDialog._apply_microphone_rows(dialog, [("A", "Mic A"), ("B", "Mic B")], "A", revision=revision)
    assert combo.currentData() == "B"                           # the newer draft survives
    SettingsDialog._apply_microphone_rows(dialog, [("A", "Mic A"), ("B", "Mic B")], "A", revision=dialog._mic_draft_rev188)
    assert combo.currentData() == "A"                           # its own revision still restores normally


@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_hist060_network_info_reaches_the_answer_without_sensitive_dumps(code):
    import socket
    from collections import namedtuple
    from jarvis.runtime.network_info188 import summary
    Addr = namedtuple("snicaddr", "family address netmask broadcast ptp")
    Stat = namedtuple("snicstats", "isup duplex speed mtu flags")
    link = getattr(socket, "AF_PACKET", getattr(socket, "AF_LINK", 17))
    data = {"hostname": "HOME-PC",
            "interfaces": {"Ethernet": [Addr(socket.AF_INET, "192.168.1.20", "255.255.255.0", None, None),
                                        Addr(link, "aa:bb:cc:dd:ee:ff", None, None, None),
                                        Addr(socket.AF_INET6, "fe80::1", None, None, None)],
                           "Wi-Fi": [Addr(socket.AF_INET, "10.0.0.5", "255.0.0.0", None, None)],
                           "lo": [Addr(socket.AF_INET, "127.0.0.1", "255.0.0.0", None, None)]},
            "stats": {"Ethernet": Stat(True, 2, 1000, 1500, ""), "Wi-Fi": Stat(False, 0, 0, 1500, ""),
                      "lo": Stat(True, 0, 0, 65536, "")},
            "partial": True, "missing": {"addresses": "gaierror"}}
    text = summary(data, code)
    assert "HOME-PC" in text and "192.168.1.20" in text and "1000" in text
    assert "aa:bb:cc" not in text and "fe80" not in text and "127.0.0.1" not in text   # no MAC / link-local / loopback
    assert "10.0.0.5" not in text                                                    # a down adapter is not listed
    assert "255.255.255.0" not in text and "snicaddr" not in text                    # no raw dump
    assert len(text.splitlines()) == 4                                                # title, host, Ethernet, partial note


def test_hist060_tool_uses_the_formatter(monkeypatch, tmp_path):
    from jarvis.testing.atlas_session import AtlasConfig, AuditAgent
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        agent = AuditAgent(AtlasConfig(tmp_path / "profile"))
        try:
            result = agent.tools._tools["network"].handler({"action": "info"})
            assert result.success and result.data.get("localized186") and "hostname" in result.data
            assert result.text.splitlines()[0].endswith(":") and "• " in result.text
        finally:
            agent.database.close()


def test_hist064_native_output_is_decoded_by_the_console_policy(monkeypatch):
    import subprocess
    from jarvis.tools import implementations
    cp866 = "Ответ от 8.8.8.8".encode("cp866")
    monkeypatch.setattr(implementations.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, cp866, b""))
    monkeypatch.setattr(implementations.os, "name", "nt")
    out = implementations._run(["ping", "-n", "1", "8.8.8.8"])
    assert out.stdout == "Ответ от 8.8.8.8" and "�" not in out.stdout
    utf8 = "Готово".encode("utf-8")
    monkeypatch.setattr(implementations.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, utf8, b""))
    assert implementations._run(["x"]).stdout == "Готово"                     # valid UTF-8 stays exact


def test_hist066_grouped_screenshots_are_found_and_pruned(tmp_path):
    import os
    from jarvis.runtime import screenshot_storage as storage
    root = tmp_path / "screenshots"
    legacy = root / "NEO_screen_old.png"
    root.mkdir()
    legacy.write_bytes(b"png")
    os.utime(legacy, (1000, 1000))
    newest = storage.screenshot_path(root, "chrome.exe")
    newest.write_bytes(b"png")
    assert newest.parent.name == "chrome" and storage.latest_screenshot(root) == newest   # not root-only
    for i in range(5):
        shot = storage.screenshot_path(root, "game.exe")
        shot.write_bytes(b"png")
        os.utime(shot, (2000 + i, 2000 + i))
    kept = root / "chrome" / "my_manual_copy.png"
    kept.write_bytes(b"png")
    removed = storage.prune_auto_screenshots(root, keep=3)
    remaining = [p for p in storage.screenshot_files(root) if p.name.startswith("NEO_screen_")]
    assert removed == 4 and len(remaining) == 3 and kept.exists()        # only automatic captures are pruned


def test_hist068_window_identity_geometry_and_ties():
    from jarvis.tools.domains.system_interaction import ambiguous_window188, window_geometry188
    win = SimpleNamespace(left=10, top=20, width=800, height=600, isMinimized=False, visible=True, title="Notes")
    assert window_geometry188(win, lambda w: 0) == (0, 10, 20, 800, 600)
    win.isMinimized = True
    assert window_geometry188(win, lambda w: 0) is None                     # minimised before the grab: refuse
    a = SimpleNamespace(title="Report - Word"); b = SimpleNamespace(title="Report - Word")
    ids = {id(a): 1, id(b): 2}
    same = lambda needle, title: 1.0 if title == "Report - Word" else 0.0
    assert ambiguous_window188("report", [a, b], same, lambda w: ids[id(w)]) == ("Report - Word", "Report - Word")
    assert ambiguous_window188("report", [a, a], same, lambda w: ids[id(w)]) is None   # one window twice is fine


def test_hist069_click_refused_after_the_window_moved_or_resized():
    from jarvis.tools.domains.system_interaction import geometry_changed188
    assert geometry_changed188((0, 0, 800, 600), (1, 0, 800, 601)) is False   # within tolerance
    assert geometry_changed188((0, 0, 800, 600), (200, 0, 800, 600)) is True  # moved
    assert geometry_changed188((0, 0, 800, 600), (0, 0, 1024, 768)) is True   # resized
    assert geometry_changed188(None, (0, 0, 800, 600)) is False               # no frame recorded: other guards apply


@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_hist074_past_activation_is_not_reported_as_the_current_model(monkeypatch, code):
    from jarvis.neo_llm import jobs
    from jarvis.tools import assistant_model_tool170 as tool
    report = {"promoted": True, "better": True, "candidate_model": "neo-agent-ft-B", "current": {"do_exact": 0.8},
              "candidate": {"do_exact": 0.9, "chat_ok": 0.9}}
    monkeypatch.setattr(jobs, "read_status", lambda config: {"last_report": report, "running": False, "status": {"stage": "done"}})
    config = SimpleNamespace(get=lambda key, default=None: {"app.language": code}.get(key, default))
    rolled = tool.run(config, {"action": "status"}, SimpleNamespace(resolve_agent_model=lambda: "neo-agent-ft-A"))
    assert "neo-agent-ft-B" in rolled.text and "neo-agent-ft-A" in rolled.text
    assert tool._t("promoted", code) not in rolled.text                          # not «the new version is in use»
    active = tool.run(config, {"action": "status"}, SimpleNamespace(resolve_agent_model=lambda: "neo-agent-ft-B:latest"))
    assert tool._t("promoted", code) in active.text


@pytest.mark.parametrize("size", [(520, 300), (420, 220)])
def test_hist076_robot_stays_inside_the_stage_from_solar_to_sleep(size):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from jarvis.ui.robot_widget import RobotWidget
    width, height = size
    bg = QColor("#0C1017")
    robot = RobotWidget(); robot.resize(width, height); robot.set_background("#0C1017"); robot.set_state("idle")

    def step(n):
        for _ in range(n):
            robot._last_tick -= 1 / 30
            robot._advance_animation()

    def lowest_robot_row():
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied); image.fill(bg)
        painter = QPainter(image); robot.render(painter, QPoint()); painter.end()
        cx = int(robot._content_rect().center().x())
        for y in range(height - 1, -1, -1):
            for x in range(cx - 60, cx + 60, 3):
                c = image.pixelColor(x, y)
                if abs(c.red() - bg.red()) + abs(c.green() - bg.green()) + abs(c.blue() - bg.blue()) > 150:
                    return y
        return -1
    step(20)
    robot.start_idle_scene(scene="solar_system", duration=20)
    step(60)
    robot.set_state("sleep")
    bottom = robot._content_rect().bottom()
    rows = []
    for _ in range(45):
        step(1)
        rows.append(lowest_robot_row())
    assert max(rows) <= bottom - 2                        # never touches/crosses the stage bottom
    robot.deleteLater()


@pytest.mark.parametrize("code", ["de", "fr", "es", "it", "pt"])
def test_hist082_plugin_permissions_are_described_in_the_ui_language(code):
    from jarvis.runtime.plugin_permissions169 import PERMISSIONS, describe
    english = {p: describe(p, "en") for p in PERMISSIONS}
    for permission in PERMISSIONS:
        text = describe(permission, code)
        assert text and text != english[permission]               # not the English prose
    assert describe("tools:*", code) != describe("tools:*", "en")


@pytest.mark.parametrize("code", ["en", "de", "fr", "es", "it", "pt"])
def test_hist082_app_owned_chat_model_error_is_localized(code):
    import re
    from jarvis.i18n import localize_runtime_text
    source = "Модель для разговора «qwen2.5:0.5b» не установлена. Откройте настройки модели и нажмите «Скачать»."
    text = localize_runtime_text(source, code)
    assert "qwen2.5:0.5b" in text and not re.search("[А-Яа-яЁё]", text)    # the literal model name is kept


def test_hist083_wave_history_records_the_real_save_outcome(tmp_path, monkeypatch):
    from jarvis.integrations.yandex_music import YandexMusicAdapter
    adapter = YandexMusicAdapter.__new__(YandexMusicAdapter)
    adapter._wave_recent_entries, adapter._wave_recent_ids = [], []
    adapter._prune_wave_recent = lambda: None
    target = tmp_path / "wave" / "history.json"
    adapter._wave_history_path = lambda: target
    adapter._persist_wave_recent_ids()
    assert adapter._wave_persist188["saved"] is True and target.exists()
    adapter._wave_history_path = lambda: tmp_path / "blocked" / "x.json"
    (tmp_path / "blocked").write_text("a file, not a folder", encoding="utf-8")
    adapter._persist_wave_recent_ids()
    assert adapter._wave_persist188["saved"] is False and adapter._wave_persist188["error"]


def test_hist083_post_start_effects_keep_their_own_outcome():
    from jarvis.integrations.yandex_music import YandexMusicAdapter
    adapter = YandexMusicAdapter.__new__(YandexMusicAdapter)
    assert adapter._after_playback_effect("ok_effect", lambda: 5) == 5
    assert adapter._after_playback_effect("bad_effect", lambda: (_ for _ in ()).throw(OSError("disk"))) is None
    assert adapter._effect_outcomes188["ok_effect"]["ok"] is True
    assert adapter._effect_outcomes188["bad_effect"] == {"ok": False, "error": "OSError: disk"}
