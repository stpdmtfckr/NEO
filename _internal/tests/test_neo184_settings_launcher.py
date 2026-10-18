"""Windows bootstrap contracts. No Windows installation or subprocess is executed."""
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

class Config:
    def __init__(self, **values): self.values = values
    def get(self, key, default=None): return self.values.get(key, default)


@pytest.mark.parametrize('enabled', (False, True))
def test_windows_install_is_always_attempted_start_is_separate(monkeypatch, enabled):
    import ensure_ollama as helper
    calls = []
    monkeypatch.setattr(helper, '_is_windows', lambda: True)
    monkeypatch.setattr(helper, '_run_windows_helper', lambda *a, **k: (calls.append(k), 0)[1])
    c = Config(**{'ollama.auto_start': enabled, 'ollama.auto_install': False, 'ollama.vision_enabled': False})
    assert helper.bootstrap(c, ROOT) == 0
    assert calls == [{'install_only': not enabled, 'cancel': False}]


@pytest.mark.parametrize('url', ('http://remote.example:11434', 'http://localhost:11434/other', 'http://token@localhost:11434', 'invalid'))
def test_nonlocal_or_invalid_endpoint_does_not_install_or_start(monkeypatch, url):
    import ensure_ollama as helper
    monkeypatch.setattr(helper, '_run_windows_helper', lambda *a, **k: pytest.fail('local setup forbidden'))
    assert helper.bootstrap(Config(**{'ollama.base_url': url}), ROOT) == 0


def test_quick_probe_has_no_install_start_side_effect(monkeypatch):
    import ensure_ollama as helper
    monkeypatch.setattr(helper, '_run_windows_helper', lambda *a, **k: pytest.fail('probe cannot setup'))
    monkeypatch.setattr(helper, 'probe_ollama', lambda *a, **k: (True, 'test'))
    assert helper.bootstrap(Config(), ROOT, quick_check=True) == 0


def test_powershell_launch_is_argument_list_bounded_no_model_download(monkeypatch):
    import ensure_ollama as helper
    calls = []
    class Result: returncode = 0
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **k: (calls.append((a, k)), Result())[1])
    assert helper._run_windows_helper(ROOT, install_only=True) == 0
    command = calls[0][0][0]
    assert isinstance(command, list) and '-InstallOnly' in command
    assert calls[0][1]['timeout'] == 1900
    source = (ROOT / 'ensure_ollama.py').read_text()
    assert 'pull_ollama_model' not in source and '/api/pull' not in source


def test_official_standalone_bootstrap_is_verified_and_non_destructive():
    source = (ROOT / 'bootstrap_ollama.ps1').read_text(encoding='utf-8-sig')
    assert 'ollama-windows-amd64.zip' in source and 'ollama-windows-arm64.zip' in source
    assert '535193f38f3344e5b08f5d1c171c31ce11aa17f0124ff69ae26d8ec7fe06fa62' in source
    assert 'Get-FileHash' in source and 'SHA256' in source
    assert 'OllamaSetup.exe' not in source and 'winget' not in source
    assert 'Stop-Process' not in source and 'taskkill' not in source.lower()
    assert '-Verb RunAs' not in source and 'Set-ExecutionPolicy' not in source
    assert 'Threading.Mutex' in source and 'ReleaseMutex' in source
    assert 'existing_server_reused' in source and 'autostart_disabled_or_install_only' in source
    assert 'Read-OllamaOptions' in source and 'settings.last-good.json' in source
    assert 'Test-Cancel' in source and 'download_network_error' in source and 'verification_failed' in source
    assert 'AddMinutes(30)' in source and 'AddSeconds(20)' in source
    assert ' pull ' not in source and '/api/pull' not in source
    assert "$env:OLLAMA_NO_CLOUD = '1'" in source


def test_cmd_queues_background_install_even_after_dependency_setup():
    text = (ROOT.parent / 'NEO.cmd').read_text()
    assert text.index('start_ollama_background.ps1') < text.index('call :ensure_deps')
    assert '-Action agent_model' not in text and '-Action model ' not in text
    assert '--cancel-ollama' in text
    assert 'if exist "%LOCALAPPDATA%\\NeoJarvis\\bootstrap\\deps184.ok" exit /b 0' in text


def test_settings_directory_survives_keep_settings_clean_install():
    text = (ROOT / 'installer_helper.ps1').read_text(encoding='utf-8-sig')
    group = text[text.index('function Get-DataGroup'):text.index('function Remove-DataGroups')]
    assert 'настройки' in group and "return 'settings'" in group
    settings = text[text.index("    'settings' {"):text.index("    'runtime' {")]
    assert "Join-Path $localRoot 'настройки'" in settings
    assert 'before-setup-' in settings and '[IO.File]::Replace' in settings


def test_cmd_all_labels_are_unique_and_subroutine_targets_exist():
    import re
    text = (ROOT.parent / 'NEO.cmd').read_text()
    labels = re.findall(r'^:([a-z_]+)$', text, re.M)
    assert len(labels) == len(set(labels))
    targets = re.findall(r'(?:goto |call :)([a-z_]+)', text)
    assert set(targets) <= set(labels)
    block = text[text.index('\n:ensure_deps\n'):text.index('\n:deps\n')]
    assert 'exit /b 0' in block and 'call :deps' in block


def test_bootstrap_log_export_scrubs_generated_keys(tmp_path):
    import zipfile
    from jarvis.runtime.diagnostics import collect_logs,redact
    public='ssh-ed25519 '+('A'*64)+' synthetic@example.invalid'
    private='-----BEGIN OPENSSH PRIVATE KEY-----\nSYNTHETIC_ONLY_NEVER_REAL\n-----END OPENSSH PRIVATE KEY-----'
    assert 'A'*64 not in redact(public)
    assert 'synthetic@example.invalid' not in redact(public)
    assert 'SYNTHETIC_ONLY' not in redact(private)
    root=tmp_path/'profile';(root/'bootstrap').mkdir(parents=True)
    (root/'bootstrap/ollama-server-error.log').write_text(public+'\n'+private+'\nserver ready')
    output=collect_logs(root,tmp_path/'diagnostics.zip')
    with zipfile.ZipFile(output) as archive:
        content=archive.read('bootstrap/ollama-server-error.log').decode()
    assert 'server ready' in content and 'REDACTED' in content
    assert 'A'*64 not in content and 'SYNTHETIC_ONLY' not in content
