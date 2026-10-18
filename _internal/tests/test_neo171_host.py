"""NEO 171: the NEO process host — NEO's processes are «NEO» with NEO's icon, not «Python».

The binary itself was run under Wine with the Windows build of CPython 3.12.10 in a real
virtual environment (see the build guide): sys.executable = Scripts\\NEO.exe, the venv is
active, workers with stdin/stdout pipes work, the exit code passes through, and a venv
created from inside the host gets the right base interpreter.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / 'launcher' / 'bin' / 'NEO.exe'


def test_host_binary_is_a_64_bit_gui_program_named_neo_with_an_icon():
    data = HOST.read_bytes()
    offset = struct.unpack_from('<I', data, 0x3C)[0]
    assert data[offset:offset + 4] == b'PE\0\0'
    assert struct.unpack_from('<H', data, offset + 4)[0] == 0x8664          # x64
    assert struct.unpack_from('<H', data, offset + 24 + 68)[0] == 2          # Windows GUI, like pythonw.exe
    for key in ('FileDescription', 'ProductName'):
        at = data.index(key.encode('utf-16-le'))
        # the value follows the key after its terminator and 32-bit padding
        assert 'NEO\x00'.encode('utf-16-le') in data[at + len(key) * 2: at + len(key) * 2 + 16], key
    assert b'Py_Main' in data and b'__PYVENV_LAUNCHER__'.decode().encode('utf-16-le') in data
    icon = (ROOT / 'assets' / 'neo_jarvis.ico').read_bytes()
    # the first icon image of neo_jarvis.ico is embedded as a resource
    count = struct.unpack_from('<H', icon, 4)[0]
    size, image_offset = struct.unpack_from('<II', icon, 6 + 8)
    assert count >= 1 and icon[image_offset:image_offset + min(size, 256)] in data


def test_manifest_matches_the_shipped_sources_and_binary():
    manifest = json.loads((ROOT / 'launcher' / 'host_manifest.json').read_text(encoding='utf-8'))
    assert manifest['binary']['sha256'] == hashlib.sha256(HOST.read_bytes()).hexdigest()
    for name in ('neo_host.c', 'neo_host.rc'):
        assert manifest['sources'][name] == hashlib.sha256((ROOT / 'launcher' / name).read_bytes()).hexdigest()


def _venv(tmp_path):
    root = tmp_path / 'runtime'
    (root / 'Scripts').mkdir(parents=True)
    (root / 'pyvenv.cfg').write_text('home = C:\\Python312\nversion = 3.12.10\n', encoding='utf-8')
    (root / 'Scripts' / 'pythonw.exe').write_bytes(b'MZ')
    (root / 'Scripts' / 'python.exe').write_bytes(b'MZ')
    return root


def test_install_copies_the_host_once_and_branded_prefers_it(tmp_path, monkeypatch):
    from jarvis.runtime import process_host171 as host
    monkeypatch.setattr(host, '_windows', lambda: True)
    root = _venv(tmp_path)
    assert host.branded(root / 'Scripts' / 'pythonw.exe') == root / 'Scripts' / 'pythonw.exe'   # before install
    installed = host.install(root)
    assert installed == root / 'Scripts' / 'NEO.exe' and installed.read_bytes() == HOST.read_bytes()
    stamp = installed.stat().st_mtime_ns
    assert host.install(root) == installed and installed.stat().st_mtime_ns == stamp               # unchanged: not copied again
    assert host.branded(root / 'Scripts' / 'pythonw.exe') == installed
    assert host.branded(root / 'Scripts' / 'python.exe') == installed
    assert host.install(tmp_path / 'not-a-venv') is None


def test_workers_start_through_the_host(tmp_path, monkeypatch):
    from jarvis.runtime import distribution164, process_host171 as host
    monkeypatch.setattr(host, '_windows', lambda: True)
    monkeypatch.delenv('NEO_HOST_EXECUTABLE', raising=False)
    root = _venv(tmp_path)
    host.install(root)
    command = distribution164.worker_command('jarvis.audio_worker', python=root / 'Scripts' / 'pythonw.exe')
    assert command[0] == str(root / 'Scripts' / 'NEO.exe') and command[1:] == ['-u', '-m', 'jarvis.audio_worker']


def test_base_executable_is_repaired_only_under_the_host(monkeypatch, tmp_path):
    from jarvis.runtime import process_host171 as host
    base = tmp_path / 'base'
    base.mkdir()
    (base / 'python.exe').write_bytes(b'MZ')
    monkeypatch.setattr(host, '_windows', lambda: True)
    monkeypatch.setattr(host.sys, 'executable', str(tmp_path / 'runtime' / 'Scripts' / 'NEO.exe'))
    monkeypatch.setattr(host.sys, '_base_executable', str(tmp_path / 'runtime' / 'Scripts' / 'NEO.exe'), raising=False)
    monkeypatch.setattr(host.sys, 'base_prefix', str(base))
    host.fix_base_executable()
    assert host.sys._base_executable == str(base / 'python.exe')


def test_neo_recognises_its_own_process_under_the_new_name():
    from jarvis.gaming.manager import GameManager
    assert 'neo.exe' in GameManager._IGNORED_PROCESSES
    source = (ROOT / 'jarvis' / 'runtime' / 'observation_policy.py').read_text(encoding='utf-8')
    assert '"neo.exe"' in source


def test_bootstrap_starts_the_assistant_through_the_host():
    source = (ROOT / 'bootstrap.py').read_text(encoding='utf-8')
    start = source.index('interpreter = Path(sys.executable).with_name("pythonw.exe")')
    block = source[start:start + 600]
    assert '_install_host(Path(sys.prefix))' in block and 'interpreter = _host' in block
