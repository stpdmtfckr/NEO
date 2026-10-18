"""Remote settings contracts: typed writes, payload isolation and live apply."""
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from jarvis.integrations.telegram_remote import TelegramRemoteController
from jarvis.integrations.telegram_settings import SETTINGS, apply_setting, display_value
from jarvis.ui.desktop_events import drain_desktop_events


class Config:
    def __init__(self):
        self.data = {"app": {"language": "en"}, "integrations": {"telegram": {"token_encrypted": "KEEP"}}, "ui": {"window_width": 322}}
        self.saves = 0

    def get(self, path, default=None):
        node = self.data
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def save(self, data):
        self.data = deepcopy(data)
        self.saves += 1


def test_remote_save_preserves_credentials_and_queues_actual_live_apply():
    drain_desktop_events(128)
    config = Config()
    spec, value, queued = apply_setting(config, "ui_window_width", "600")
    assert spec.path == "ui.window_width" and value == 600 and queued
    assert config.data["integrations"]["telegram"]["token_encrypted"] == "KEEP"
    assert config.get("ui.window_width") == 600
    assert drain_desktop_events() == [{"action": "settings.apply"}]


@pytest.mark.parametrize("sid,value", [
    ("integrations.telegram.token_encrypted", "x"), ("security_allow_everything", "1"),
    ("ui_window_width", "-1"), ("ui_window_width", "7000"), ("ui_window_width", "float('nan')"),
    ("routing_semantic_threshold", "nan"), ("routing_semantic_threshold", "inf"),
    ("ui_robot_only_mode", "true"), ("ui_robot_theme_preset", "999"),
    ("ui_accent", "red; command"), ("hotkeys_bookshelf_toggle", "__import__('os')"),
    ("activity_excluded_apps", "process\x00.exe"),
])
def test_untrusted_setting_ids_and_invalid_values_never_write(sid, value):
    config = Config()
    original = deepcopy(config.data)
    with pytest.raises(ValueError):
        apply_setting(config, sid, value)
    assert config.data == original and config.saves == 0


def test_remote_presets_apply_palette_and_transparency_mirror():
    config = Config()
    index = SETTINGS["ui_robot_theme_preset"].choices.index("NEO Orange")
    apply_setting(config, "ui_robot_theme_preset", str(index))
    assert config.get("ui.robot_eye_color") == "#FF9838"
    assert config.get("ui.robot_accessory_hand_color") == config.get("ui.robot_body_color")
    apply_setting(config, "ui_window_opacity_percent", "0")
    assert config.get("ui.transparent_background") is True
    apply_setting(config, "ui_accent", "#1122aa")
    assert config.get("ui.window_theme_preset") == "Custom"
    assert config.get("ui.accent") == "#1122AA"


def test_remote_list_setting_typed_bounded_and_explicit_clear():
    config = Config()
    apply_setting(config, "activity_excluded_apps", "Game.exe\nGame.exe\nEditor.exe")
    assert config.get("activity.excluded_apps") == ["Game.exe", "Editor.exe"]
    apply_setting(config, "activity_excluded_apps", "-")
    assert config.get("activity.excluded_apps") == []
    with pytest.raises(ValueError):
        apply_setting(config, "activity_excluded_apps", "\n".join(str(n) for n in range(33)))


def test_remote_callback_edits_setting_without_opening_desktop_dialog(monkeypatch):
    controller = object.__new__(TelegramRemoteController)
    controller.config = Config()
    controller._lock = threading.RLock()
    controller._seen_callback_ids = {}
    controller._track_message = lambda *a, **k: None
    controller._is_authorized = lambda *a: True
    controller._answer_callback = lambda *a: None
    panels, sent = [], []
    controller._panel = lambda *a, **k: panels.append((a, k))
    controller._send_message = lambda *a, **k: sent.append(a)
    controller._tg_text = lambda text: text
    controller._execute_tool = lambda *a, **k: pytest.fail("Settings must edit remotely")
    callback = {"id": "unique", "from": {"id": 7}, "message": {"message_id": 1, "chat": {"id": 7, "type": "private"}}, "data": "a:ncfg:ui_robot_only_mode:1"}
    controller._handle_callback(callback)
    assert controller.config.get("ui.robot_only_mode") is True
    assert len(sent) == 1 and "queued" in sent[0][1]
    assert panels and panels[0][1]["view"].startswith("ncfg:window:")
    controller._handle_callback(callback)
    assert controller.config.saves == 1  # duplicate callback cannot write twice


def test_remote_unauthorized_callback_cannot_change_settings():
    controller = object.__new__(TelegramRemoteController)
    controller.config = Config()
    controller._is_authorized = lambda *a: False
    controller._answer_callback = lambda *a: None
    controller._tg_message = lambda text: text
    controller._handle_callback({"data": "a:ncfg:ui_robot_only_mode:1"})
    assert controller.config.saves == 0


def test_all_setting_callbacks_fit_telegram_limit():
    for setting in SETTINGS.values():
        assert len(("a:ncfg:" + setting.id + ":100").encode("utf-8")) <= 64


def test_every_remote_setting_and_enum_has_native_labels():
    from jarvis.integrations.telegram_setting_labels import LABELS, CHOICES
    for setting in SETTINGS.values():
        assert setting.title in LABELS
        assert len(LABELS[setting.title]) == 6
        for code in ("ru", "en", "de", "fr", "es", "it", "pt"):
            assert setting.label(code)
            for choice in setting.choices:
                assert display_value(setting, choice, code)
    assert CHOICES["street"][2] != "street"
