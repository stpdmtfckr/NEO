"""NEO 176: uninstall and clean install delete exactly the chosen groups of NEO's data.

Bug: in Windows PowerShell 5.1 a native tool writing to stderr (schtasks: «task not found»)
stopped the whole removal at its first step, and groups were defined by what to keep, so
caches, the Yandex Music history and other files stayed.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

HELPER = Path(__file__).resolve().parents[1] / 'installer_helper.ps1'
SOURCE = HELPER.read_text(encoding='utf-8-sig')


def _action(name):
    start = SOURCE.index(f"    '{name}' {{")
    end = SOURCE.index("\n    '", start + 10) if "\n    '" in SOURCE[start + 10:] else len(SOURCE)
    return SOURCE[start:end]


def test_a_failing_step_never_stops_the_removal():
    for name in ('remove', 'clean'):
        block = _action(name)
        assert "$ErrorActionPreference = 'Continue'" in block
        assert not re.search(r'&\s*\S+\s.*2>\$null', block), 'native stderr must not reach PowerShell'
    assert 'Invoke-Quiet "schtasks.exe' in _action('remove')


def test_every_data_entry_belongs_to_a_group_and_groups_are_removed_by_choice():
    assert 'function Get-DataGroup' in SOURCE and 'function Remove-DataGroups' in SOURCE
    assert 'Remove-DataGroups' in _action('remove') and 'Remove-DataGroups' in _action('clean')
    assert 'yandex_wave_recent' in SOURCE                        # listening history: «cache» group


FILES = ('settings.json', 'assistant.db', 'overlays.json', 'adaptive_memory.sqlite3', 'learned_phrases.json', 'yandex_wave_recent.json',
         'yandex_wave_recent.json.lock', 'game_sessions.json', 'daily_briefing_state.json', 'telegram_remote_state.json',
         'setup.log', 'NEO_Piper_Assistant_Voices.zip')
DIRS = ('backups', 'memory', 'history', 'shelf', 'logs', 'diagnostics', 'screenshots', 'speech', 'voice_packs', 'models',
        'llm_train', 'cache', 'media_sessions', 'instance', 'neo_model', 'knowledge', 'exports', 'yandex_audio_cache',
        'thumbnails', 'bootstrap', 'runtime')


def _run(tmp_path, *args):
    pwsh = shutil.which('pwsh') or shutil.which('powershell')
    if not pwsh:
        pytest.skip('PowerShell is not available here')
    root = tmp_path / 'local' / 'NeoJarvis'
    for name in DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / 'x.txt').write_text('x')
    for name in FILES:
        (root / name).write_text('x')
    for marker in ('ollama_install.json', 'ollama-server.log'):
        (root / 'bootstrap' / marker).write_text('{}')
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path / 'local'), APPDATA=str(tmp_path / 'roaming'))
    subprocess.run([pwsh, '-NoProfile', '-File', str(HELPER), *args], env=env, capture_output=True, timeout=120)
    return {p.name for p in root.iterdir()} if root.exists() else set()


def test_uninstall_everything_except_screenshots_and_voices(tmp_path):
    left = _run(tmp_path, '-Action', 'remove', '-What', 'startup,runtime,settings,history,logs,cache,other,model')
    assert left == {'bootstrap', 'screenshots', 'speech', 'voice_packs', 'models', 'NEO_Piper_Assistant_Voices.zip'}
    # Ollama stays on the computer, so does the marker that NEO installed it.
    assert {p.name for p in (tmp_path / 'local/NeoJarvis/bootstrap').iterdir()} == {'ollama_install.json'}


def test_uninstall_keeping_only_the_settings(tmp_path):
    """NEO 177: «keep settings» kept chat history, memory and install markers too."""
    left = _run(tmp_path, '-Action', 'remove', '-What', 'startup,runtime,history,logs,screenshots,cache,other,speech,model,ollama,python')
    assert left == {'settings.json', 'overlays.json', 'backups'}


def test_clean_install_keeping_settings(tmp_path):
    left = _run(tmp_path, '-Action', 'clean', '-Keep', 'settings')
    assert left == {'bootstrap', 'settings.json', 'overlays.json', 'backups'}
    left = _run(tmp_path, '-Action', 'clean', '-Keep', 'settings,history')
    assert left == {'bootstrap', 'settings.json', 'assistant.db', 'overlays.json', 'adaptive_memory.sqlite3',
                    'learned_phrases.json', 'backups', 'memory', 'history', 'shelf'}


def test_full_uninstall_leaves_nothing(tmp_path):
    assert _run(tmp_path, '-Action', 'remove', '-What', 'startup,runtime,everything') == set()
