"""NEO185: Save keeps untouched values (QA cp31 late findings 1-5, LATER-08/09/12)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from jarvis.config import ConfigManager
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ('HOME', 'LOCALAPPDATA', 'APPDATA', 'XDG_CACHE_HOME'):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(ConfigManager, 'apply_wake_service', lambda *_: None)
    monkeypatch.setattr(ConfigManager, 'apply_autostart', lambda *_: None)
    with external_io_blocked(), patch.object(socket.socket, 'sendto', side_effect=PermissionError('QA blocks datagrams')), patch.object(os, 'system', side_effect=PermissionError('QA blocks shell')):
        yield


def config():
    return ConfigManager(ROOT / 'config/default_settings.json')


@pytest.fixture(scope='module')
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _dialog(app, monkeypatch, c):
    from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
    monkeypatch.setattr(SettingsDialog, '_refresh_microphones_async', lambda *_: None)
    monkeypatch.setattr(SettingsDialog, '_start_game_cache_stats158', lambda *_: None)
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: None)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)
    d = SettingsDialog(c)
    assert d._settings_ready160 and not d._settings_build_error160
    return d


def _close(app, d):
    d.reject()
    d.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_cosmetic_save_keeps_voice_telegram_style_and_unlock_values(app, monkeypatch):
    c = config()
    c.data['voice']['silence_seconds'] = 1.05
    c.data['voice']['post_answer_rearm_ms'] = 12
    c.data['voice']['continuous_rearm_ms'] = 20
    c.data['voice']['start_timeout_seconds'] = 4.0
    c.data['wake_word']['chunk_seconds'] = 0.95
    c.data['integrations']['telegram']['min_command_interval_seconds'] = 0.15
    c.data['integrations']['yandex_music']['background_playback'] = False
    c.data['integrations']['discord']['background_bot_enabled'] = False
    notes = c.data.setdefault('desktop', {}).setdefault('overlay', {}).setdefault('styles', {}).setdefault('notes', {})
    notes.update(padding=22, border_width=3)
    c.data.setdefault('voice_unlock', {}).update(enabled=False, tscon_fallback=False)
    c.save(c.data)
    d = _dialog(app, monkeypatch, c)
    try:
        d.corner_radius.setValue(d.corner_radius.value() + 2)
        d._save(close=False)
    finally:
        _close(app, d)
    fresh = config()
    assert fresh.get('voice.silence_seconds') == 1.05
    assert fresh.get('voice.post_answer_rearm_ms') == 12
    assert fresh.get('voice.continuous_rearm_ms') == 20
    timeout = fresh.get('voice.start_timeout_seconds')
    assert timeout == 4.0 and isinstance(timeout, float)
    assert fresh.get('wake_word.chunk_seconds') == 0.95
    assert fresh.get('integrations.telegram.min_command_interval_seconds') == 0.15
    assert fresh.get('integrations.yandex_music.background_playback') is False
    assert fresh.get('integrations.discord.background_bot_enabled') is False
    assert fresh.get('desktop.overlay.styles.notes.padding') == 22
    assert fresh.get('desktop.overlay.styles.notes.border_width') == 3
    assert fresh.get('voice_unlock.tscon_fallback') is False


def test_changed_numeric_field_is_still_saved(app, monkeypatch):
    c = config()
    c.data['voice']['silence_seconds'] = 1.05
    c.save(c.data)
    d = _dialog(app, monkeypatch, c)
    try:
        d.silence_seconds.setValue(1.5)
        d.telegram_command_interval.setValue(0.10)
        d._save(close=False)
    finally:
        _close(app, d)
    fresh = config()
    assert fresh.get('voice.silence_seconds') == 1.5
    assert abs(fresh.get('integrations.telegram.min_command_interval_seconds') - 0.10) < 1e-9


def test_legacy_migration_keeps_disabled_yandex_and_discord_background():
    root = Path(os.environ['LOCALAPPDATA']) / 'NeoJarvis'
    root.mkdir(parents=True)
    payload = {'settings_version': 55, 'integrations': {
        'yandex_music': {'background_playback': False, 'background_volume_percent': 37},
        'discord': {'background_bot_enabled': False}}}
    (root / 'settings.json').write_text(json.dumps(payload), encoding='utf-8')
    c = config()
    assert c.get('integrations.yandex_music.background_playback') is False
    assert c.get('integrations.discord.background_bot_enabled') is False
    assert c.get('integrations.yandex_music.ui_fallback') is False
    c.save(c.data)
    fresh = config()
    assert fresh.get('integrations.yandex_music.background_playback') is False
    assert fresh.get('integrations.discord.background_bot_enabled') is False
    assert fresh.get('integrations.yandex_music.background_volume_percent') == 37


def test_discord_bot_respects_master_and_background_switches():
    from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
    c = config()
    bot = DiscordBackgroundBot(c)
    c.data['integrations']['discord']['enabled'] = True
    c.data['integrations']['discord']['background_bot_enabled'] = True
    assert bot._enabled()
    c.data['integrations']['discord']['enabled'] = False
    assert not bot._enabled()
    with pytest.raises(RuntimeError):
        bot.ensure_ready(timeout=0.1)
    c.data['integrations']['discord']['enabled'] = True
    c.data['integrations']['discord']['background_bot_enabled'] = False
    assert not bot._enabled()
    assert bot._inactive(object())


def test_gateway_does_not_reconnect_when_disabled():
    import asyncio
    import threading
    from jarvis.integrations.background.gateway_recovery import run_gateway

    class Owner:
        def __init__(self):
            self._closing = threading.Event(); self._ready = threading.Event(); self._failed = threading.Event()
            self._lock = threading.Lock(); self._client = None; self._error = ''
        def _enabled(self):
            return False

    calls = []
    owner = Owner()
    asyncio.run(run_gateway(owner, lambda: calls.append(1), 'token'))
    assert calls == [] and owner._failed.is_set() and owner._gateway_phase == 'disabled'


@pytest.mark.parametrize('code,ask,answer,purpose', [
    ('en', 'open application', 'cancel shutdown', 'clarification'),
    ('en', 'open application', 'Cancel Shutdown', 'clarification'),
    ('ru', 'открой приложение', 'cancel shutdown', 'clarification'),
    ('en', 'open application', 'cancel', 'cancelled'),
    ('en', 'open application', 'cancel it', 'cancelled'),
    ('en', 'open application', 'cancel that please', 'cancelled'),
    ('en', 'open application', 'never mind', 'cancelled'),
    ('de', 'öffne die Anwendung', 'abbrechen', 'cancelled'),
    ('fr', 'ouvre l’application', 'annule', 'cancelled'),
])
def test_nb42_cancel_word_inside_application_name(code, ask, answer, purpose):
    from jarvis.runtime.dialogue_tasks import prepare_dialogue
    from jarvis.runtime.conversation import ConversationState
    from jarvis.runtime.feature_commands import parse_feature_command
    state = ConversationState()
    parse = lambda text: parse_feature_command(text, code)
    assert prepare_dialogue(ask, code, state, parse, now=1).purpose == 'clarification'
    decision = prepare_dialogue(answer, code, state, parse, now=2)
    assert decision.purpose == purpose
    if purpose == 'clarification':
        assert decision.feature == ('application', {'action': 'open', 'name': answer})
