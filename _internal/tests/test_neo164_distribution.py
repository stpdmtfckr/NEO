"""Native-host routing without arbitrary Python CLI or source-file workers."""
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from jarvis.runtime import distribution164 as dist
import neo_entry164 as entry


@pytest.fixture
def native(monkeypatch, tmp_path):
    host = tmp_path/'Installed'/'NEO.exe'
    root = tmp_path/'Packages'/'164'
    runtime = tmp_path/'runtime'/'v4'
    monkeypatch.setenv('NEO_HOST_EXECUTABLE', str(host))
    monkeypatch.setenv('NEO_PACKAGE_ROOT', str(root))
    monkeypatch.setenv('NEO_RUNTIME_ROOT', str(runtime))
    return host, root, runtime


def test_native_uses_installed_host_and_private_interpreter(native):
    host, root, runtime = native
    assert dist.project_root() == host.parent
    assert dist.console_python() == runtime/'Scripts'/'python.exe'


def test_native_worker_uses_host_with_a_private_runtime(native):
    host, _, _ = native
    python = host.parent/'runtimes'/'example'/'Scripts'/'python.exe'
    assert dist.worker_command('jarvis.stt_worker', python=python) == [
        str(host),'--neo-runtime',str(python.parent.parent),'-u','-m','jarvis.stt_worker']


@pytest.mark.parametrize('module', [
    'jarvis.audio_worker', 'jarvis.stt_worker', 'jarvis.wake_worker',
    'jarvis.ambient_loopback_worker',
    'jarvis.integrations.background.local_player_worker',
    'jarvis.runtime.discord_audio_meter_worker',
])
def test_native_isolated_workers_are_started_by_branded_host(native, module):
    host, _, _ = native
    assert dist.worker_command(module) == [str(host), '-u', '-m', module]


@pytest.mark.parametrize('argv', [['-c','print(1)'],['-m','os'],['--neo-run','os'],['script.py'],['--neo-runtime']])
def test_host_does_not_expose_general_python_cli(argv):
    assert entry.dispatch(argv) == 64


def test_worker_dispatch_preserves_args_and_main_return(monkeypatch, native):
    observed = []
    def main():
        observed.append(sys.argv[:]);return 7
    monkeypatch.setattr(entry.importlib, 'import_module', lambda module: SimpleNamespace(main=main))
    monkeypatch.setattr(sys,'argv',['NEO.exe'])
    assert entry.dispatch(['--neo-runtime','C:/runtime','-u','-m','jarvis.audio_worker','--list-devices']) == 7
    assert observed == [['jarvis.audio_worker','--list-devices']]


def test_prepare_data_does_not_import_ui(monkeypatch, native, capsys):
    monkeypatch.setattr(entry.importlib,'import_module',lambda _: pytest.fail('unexpected import'))
    assert entry.dispatch(['--prepare-data']) == 0
    assert capsys.readouterr().out.strip() == str(native[1])


@pytest.mark.parametrize('flag', ['--show', '--autostart', '--remote-start'])
def test_supported_launcher_aliases_start_app(monkeypatch, flag):
    calls = []
    monkeypatch.setitem(sys.modules, 'app', SimpleNamespace(main=lambda: calls.append(sys.argv[:]) or 0))
    monkeypatch.setattr(sys, 'argv', ['NEO.exe', flag])
    assert entry.dispatch([flag]) == 0
    assert calls == [[sys.executable]]
