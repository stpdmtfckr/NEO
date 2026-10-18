"""NEO 171: «Automatically turn Instant Replay on at game start» (Settings → Games)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from tests.test_neo169_instant_replay import FakeNvidia, cfg, replay
from jarvis.integrations.instant_replay169 import ReplayWatch

AUTO = {"gaming.instant_replay.auto_enable": True}


def watch(nvidia, overlay=True, **settings):
    config = cfg(**settings)
    return ReplayWatch(config, replay(nvidia, config, overlay=overlay))


def test_off_at_game_start_is_turned_on_verified_and_reported():
    nvidia = FakeNvidia(False)
    text = watch(nvidia, **AUTO).game_started("Dota 2")
    assert nvidia.pressed == ["alt+shift+f10"] and nvidia.state is True
    assert text == "Запущена Dota 2: мгновенный повтор NVIDIA был выключен — я его включил (ALT+SHIFT+F10)."


def test_no_extra_change_notice_after_the_auto_enable():
    nvidia = FakeNvidia(False)
    w = watch(nvidia, **AUTO)
    w.game_started("Dota 2")
    assert w.poll() is None


def test_already_on_or_unknown_is_never_pressed():
    for state in (True, None):
        nvidia = FakeNvidia(state)
        assert watch(nvidia, **AUTO).game_started("Dota 2") is None
        assert nvidia.pressed == []           # a toggle is never pressed blind


def test_failure_is_reported_honestly():
    nvidia = FakeNvidia(False, responds=False)
    text = watch(nvidia, **AUTO).game_started("Dota 2")
    assert text.startswith("⚠ Запущена Dota 2, мгновенный повтор NVIDIA выключен, и включить его автоматически не получилось.")
    assert "состояние мгновенного повтора не изменилось" in text
    nvidia = FakeNvidia(False)
    text = watch(nvidia, overlay=False, **AUTO).game_started("Dota 2")
    assert nvidia.pressed == [] and 'оверлей NVIDIA не запущен' in text  # startup warning; never assume OFF


def test_setting_off_keeps_the_warning_with_a_hint():
    nvidia = FakeNvidia(False)
    text = watch(nvidia).game_started("Dota 2")
    assert nvidia.pressed == [] and "мгновенный повтор NVIDIA выключен" in text
    assert "Автоматически включать мгновенный повтор" in text
    assert watch(FakeNvidia(False), **AUTO, **{"gaming.enabled": False}).game_started("Dota 2") is None


def test_with_notify_it_runs_in_the_background_and_reports_there():
    nvidia = FakeNvidia(False)
    got, done = [], threading.Event()
    result = watch(nvidia, **AUTO, **{"app.language": "en"}).game_started("Dota 2", notify=lambda t: (got.append(t), done.set()))
    assert result is None and done.wait(5)
    assert got == ["Dota 2 started: NVIDIA Instant Replay was off — I turned it on (ALT+SHIFT+F10)."]


def test_every_language_has_the_new_messages():
    from jarvis.integrations.instant_replay169 import MESSAGES
    for key in ("auto_on", "auto_failed", "auto_pressed", "auto_hint"):
        assert len(MESSAGES[key]) == 7 and all(MESSAGES[key])


def test_settings_default_migration_dialog_and_translations():
    from jarvis.runtime import settings171
    root = Path(__file__).resolve().parents[1]
    defaults = json.loads((root / "config" / "default_settings.json").read_text(encoding="utf-8"))
    assert defaults["gaming"]["instant_replay"]["auto_enable"] is False and defaults["settings_version"] >= 171
    data = {"gaming": {"instant_replay": {"toggle_hotkey": "alt+f10"}}, "settings_version": 170}
    settings171.migrate(data, 170)
    assert data["gaming"]["instant_replay"] == {"toggle_hotkey": "alt+f10", "auto_enable": False} and data["settings_version"] == 171
    data["gaming"]["instant_replay"]["auto_enable"] = True
    settings171.migrate(data, 171)
    assert data["gaming"]["instant_replay"]["auto_enable"] is True
    source = (root / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    label = "Автоматически включать мгновенный повтор при запуске игры, если он точно выключен"
    assert label in source and 'gaming169["instant_replay"]["auto_enable"] = self.replay_auto_enable171.isChecked()' in source
    from jarvis.ui.settings_text184 import ROWS
    assert len(ROWS[label]) == 6 and all(ROWS[label])


def test_game_tracker_passes_its_notice_to_the_watch():
    source = (Path(__file__).resolve().parents[1] / "jarvis" / "gaming" / "memory.py").read_text(encoding="utf-8")
    assert "game_started(game_name, notify=self._notice, session=key)" in source
