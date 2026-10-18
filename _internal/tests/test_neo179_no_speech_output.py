"""NEO 179: NEO does not speak answers — Piper, its voices and every voice-output setting are gone."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_old_settings_lose_the_voice_output_section():
    from jarvis.runtime import settings179
    data = {'voice': {'enabled': True, 'tts': {'enabled': True, 'provider': 'piper'}},
            'gaming': {'match_ready': {'enabled': True, 'speak': True, 'telegram': True}}}
    settings179.migrate(data, 178)
    assert data == {'voice': {'enabled': True}, 'gaming': {'match_ready': {'enabled': True, 'telegram': True}}}


def test_saved_178_settings_load_without_voice_output(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    saved = tmp_path / 'NeoJarvis' / 'settings.json'
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({'settings_version': 178, 'voice': {'enabled': True, 'tts': {'enabled': True, 'provider': 'piper'}},
                                 'gaming': {'match_ready': {'speak': True, 'telegram': False}}}), encoding='utf-8')
    from jarvis.config import ConfigManager
    config = ConfigManager(ROOT / 'config' / 'default_settings.json')
    assert config.get('voice.tts') is None and config.get('gaming.match_ready.speak') is None
    assert config.get('voice.enabled') is True and config.get('gaming.match_ready.telegram') is False
    assert config.get('settings_version') == 179
    defaults = json.loads((ROOT / 'config' / 'default_settings.json').read_text(encoding='utf-8'))
    assert defaults['settings_version'] == 179
    assert 'tts' not in defaults['voice'] and 'speak' not in defaults['gaming']['match_ready']


def test_downloaded_voices_and_piper_are_deleted(tmp_path):
    from jarvis.runtime.settings179 import remove_old_voices
    tts = tmp_path / 'models' / 'tts'
    (tts / 'ru_RU-dmitri-medium').mkdir(parents=True)
    (tts / 'ru_RU-dmitri-medium' / 'model.onnx').write_bytes(b'x' * 10)
    (tts / 'runtime-cp312' / 'Scripts').mkdir(parents=True)
    (tmp_path / 'settings.json').write_text('{}')
    assert remove_old_voices(tmp_path) is True
    assert not tts.exists() and not (tmp_path / 'models').exists()
    assert (tmp_path / 'settings.json').exists()          # nothing else is touched
    assert remove_old_voices(tmp_path) is False


def test_other_models_stay(tmp_path):
    from jarvis.runtime.settings179 import remove_old_voices
    (tmp_path / 'models' / 'tts').mkdir(parents=True)
    (tmp_path / 'models' / 'other').mkdir()
    remove_old_voices(tmp_path)
    assert (tmp_path / 'models' / 'other').is_dir()


def test_no_speech_output_code_or_data_is_left():
    assert not (ROOT / 'jarvis' / 'speech_out').exists()
    assert not (ROOT / 'jarvis' / 'ui' / 'voice_output_page.py').exists()
    for name in ('tts_presets.json', 'tts_assistant_pack.json', 'canonical_phrases/speech150.json', 'command_variants/speech150.json'):
        assert not (ROOT / 'config' / name).exists(), name
    for path in list((ROOT / 'jarvis').rglob('*.py')) + [ROOT / 'bootstrap.py', ROOT / 'neo_entry164.py']:
        text = path.read_text(encoding='utf-8')
        assert 'speech_out' not in text and 'voice.tts' not in text.replace('``voice.tts``', ''), path
    for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
        catalogue = json.loads((ROOT / 'jarvis' / 'locales' / 'commands' / f'{code}.json').read_text(encoding='utf-8'))
        assert not [a for a in catalogue['action_phrases'] if a.startswith('speech.')]
        assert catalogue['action_count'] == len(catalogue['action_phrases'])


def test_installer_helper_downloads_no_voices():
    helper = (ROOT / 'installer_helper.ps1').read_text(encoding='utf-8-sig')
    assert "'voices'" not in helper and 'speech_out' not in helper and '$Languages' not in helper


def test_settings_have_no_voice_page_or_speak_switch():
    source = (ROOT / 'jarvis' / 'ui' / 'settings_dialog.py').read_text(encoding='utf-8')
    for gone in ('voice_output', 'match_ready_speak169', 'озвучивать', 'Голос, которым NEO отвечает'):
        assert gone not in source, gone


def test_portuguese_reminder_in_minutes_is_a_reminder_not_memory():
    # «lembra-me» (remind me) used to be read as «lembre» (remember) and was not understood.
    from jarvis.runtime.multilingual_commands import normalize_multilingual_command
    from jarvis.tools.integrations import _extract_duration_seconds, _timer_label
    text = normalize_multilingual_command('lembra-me daqui a 10 minutos: ver o forno', 'pt')
    assert text.startswith('напоминание')
    assert _extract_duration_seconds(text) == 600
    assert _timer_label(text, 'reminder') == 'ver o forno'
