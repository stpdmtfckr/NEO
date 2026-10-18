"""NEO 188 HREQ-020/021: temperature and process-load notices, Steam installs, LibreHardwareMonitor."""
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.runtime import lhm188
from jarvis.runtime import system_watch188 as sw

CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def test_temperature_alert_needs_two_readings_and_has_hysteresis_and_cooldown():
    alerts = sw.HardwareAlerts(cooldown=100)
    th = {'cpu': 90, 'gpu': 85}
    assert alerts.observe({'cpu': 92, 'gpu': None}, th, now=0) == []                       # one reading is not enough
    events = alerts.observe({'cpu': 93, 'gpu': None}, th, now=10)
    assert events == [('hardware.temp_high', {'component': 'cpu', 'value': 93.0, 'threshold': 90.0})]
    assert alerts.observe({'cpu': 95}, th, now=20) == []                                     # cooldown
    assert alerts.observe({'cpu': 88}, th, now=30) == []                                     # not 5 °C below yet
    assert alerts.observe({'cpu': 84}, th, now=40) == []
    normal = alerts.observe({'cpu': 83}, th, now=50)
    assert normal and normal[0][0] == 'hardware.temp_normal'
    assert alerts.observe({'gpu': None, 'cpu': None}, th, now=60) == []                       # unavailable sensors are silent


def test_process_spike_needs_two_samples_and_one_notice_per_cooldown():
    spikes = sw.ProcessSpikes(cooldown=100)
    rows = [{'pid': 10, 'name': 'game.exe', 'cpu': 75.0, 'ram_mb': 900.0}, {'pid': 11, 'name': 'idle.exe', 'cpu': 1.0, 'ram_mb': 50.0}]
    assert spikes.observe(rows, cpu_percent=60, ram_mb=4096, now=0) == []
    events = spikes.observe(rows, cpu_percent=60, ram_mb=4096, now=5)
    assert events == [('process.spike', {'process': 'game.exe', 'pid': 10, 'metric': 'cpu', 'value': 75.0, 'threshold': 60.0})]
    assert spikes.observe(rows, cpu_percent=60, ram_mb=4096, now=10) == []
    heavy = [{'pid': 12, 'name': 'browser.exe', 'cpu': 2.0, 'ram_mb': 6000.0}]
    spikes.observe(heavy, cpu_percent=60, ram_mb=4096, now=11)
    assert spikes.observe(heavy, cpu_percent=60, ram_mb=4096, now=12)[0][1]['metric'] == 'ram'


def test_process_rows_measure_cpu_between_calls():
    rows, previous = sw.process_rows({})
    assert all(r['cpu'] is None for r in rows)
    rows2, _ = sw.process_rows(previous)
    assert any(r['cpu'] is not None for r in rows2) and all((r['cpu'] or 0) >= 0 for r in rows2)


def manifest(folder, appid, name, flags, need, got):
    (folder / f'appmanifest_{appid}.acf').write_text(
        '"AppState"\n{\n\t"appid"\t\t"%s"\n\t"name"\t\t"%s"\n\t"StateFlags"\t\t"%d"\n\t"installdir"\t\t"%s"\n'
        '\t"BytesToDownload"\t\t"%d"\n\t"BytesDownloaded"\t\t"%d"\n}\n' % (appid, name, flags, name, need, got), encoding='utf-8')


def test_steam_completion_only_from_the_manifest(tmp_path):
    lib = tmp_path / 'steamapps'
    lib.mkdir()
    manifest(lib, 1, 'Old Game', 4, 10, 10)
    steam = sw.SteamInstalls()
    assert steam.observe([lib]) == []                                     # what existed before is not news
    manifest(lib, 2, 'New Game', 1026, 1000, 100)
    started = steam.observe([lib])
    assert started == [('installation.steam_started', {'name': 'New Game', 'path': str(lib / 'common' / 'New Game'), 'source': 'steam'})]
    manifest(lib, 2, 'New Game', 1026, 1000, 1000)                       # all bytes, but Steam has not marked it installed
    assert steam.observe([lib]) == []
    manifest(lib, 2, 'New Game', 4, 1000, 1000)
    done = steam.observe([lib])
    assert done[0][0] == 'installation.steam_completed' and done[0][1]['name'] == 'New Game'
    assert steam.observe([lib]) == []


def test_steam_library_folders_are_read(tmp_path):
    (tmp_path / 'steamapps').mkdir()
    other = tmp_path / 'D' / 'SteamLibrary'
    (other / 'steamapps').mkdir(parents=True)
    (tmp_path / 'steamapps' / 'libraryfolders.vdf').write_text('"libraryfolders"\n{\n "1"\n {\n  "path"  "%s"\n }\n}\n' % str(other).replace('\\', '\\\\'), encoding='utf-8')
    libs = sw.steam_libraries(tmp_path)
    assert tmp_path / 'steamapps' in libs and other / 'steamapps' in libs


@pytest.mark.parametrize('code', CODES)
def test_texts_in_seven_languages(code):
    assert '93' in sw.event_text('hardware.temp_high', {'component': 'cpu', 'value': 93.0, 'threshold': 90}, code)
    assert 'game.exe' in sw.event_text('process.spike', {'process': 'game.exe', 'metric': 'cpu', 'value': 75, 'threshold': 60}, code)
    assert 'New Game' in sw.event_text('installation.steam_completed', {'name': 'New Game', 'path': 'C:/x'}, code)
    from jarvis.runtime.desktop_services import event_text
    assert event_text({'message_key': 'hardware.temp_normal', 'data': {'component': 'gpu', 'value': 70, 'threshold': 85}}, code)
    for key in lhm188._T:
        assert lhm188._T[key][CODES.index(code)]


def test_watchers_run_only_enabled_parts(monkeypatch):
    calls = []
    monkeypatch.setattr(sw, 'hardware_readings', lambda: calls.append('hw') or {'cpu': 50.0, 'gpu': None})
    monkeypatch.setattr(sw, 'process_rows', lambda prev: calls.append('proc') or ([], {}))
    monkeypatch.setattr(sw, 'steam_root', lambda: calls.append('steam') or None)
    values = {'monitoring.hardware_alerts_enabled': True}
    cfg = SimpleNamespace(get=lambda k, d=None: values.get(k, d))
    w = sw.Watchers()
    assert w.enabled(cfg)
    w.run(cfg, now=0)
    assert calls == ['hw']
    assert not w.enabled(SimpleNamespace(get=lambda k, d=None: d))
    assert w.interval(SimpleNamespace(get=lambda k, d=None: 1)) == 15.0


def lhm_zip(names):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        for name in names:
            z.writestr(name, b'x')
    return zipfile.ZipFile(io.BytesIO(buffer.getvalue()))


@pytest.mark.parametrize('bad', ['../evil.exe', '/abs.exe', 'C:/x.exe', 'a/../../b.exe'])
def test_lhm_archive_cannot_escape_its_folder(tmp_path, bad):
    with pytest.raises(lhm188.LhmError):
        lhm188.safe_extract(lhm_zip(['LibreHardwareMonitor.exe', bad]), tmp_path / 'out')


def test_lhm_asset_must_come_from_the_official_release():
    good = {'tag_name': 'v1', 'assets': [{'name': 'LibreHardwareMonitor-net472.zip',
                                          'browser_download_url': 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v1/LibreHardwareMonitor-net472.zip'}]}
    assert lhm188.choose_asset(good)[1] == 'v1'
    evil = {'assets': [{'name': 'LibreHardwareMonitor-net472.zip', 'browser_download_url': 'https://example.com/LibreHardwareMonitor-net472.zip'}]}
    with pytest.raises(lhm188.LhmError):
        lhm188.choose_asset(evil)


class Response:
    def __init__(self, data=None, body=b''):
        self.data, self.body, self.headers = data, body, {'content-length': str(len(body))}

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def iter_content(self, size):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_lhm_download_check_and_remove_only_own_copy(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('LibreHardwareMonitor.exe', b'MZ')
        z.writestr('LibreHardwareMonitorLib.dll', b'MZ')
    release = {'tag_name': 'v9', 'assets': [{'name': 'LibreHardwareMonitor-net472.zip',
                                             'browser_download_url': 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v9/LibreHardwareMonitor-net472.zip'}]}
    session = SimpleNamespace(get=lambda url, **kw: Response(release) if 'api.github.com' in url else Response(body=buffer.getvalue()))
    cfg = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda k, d=None: d)
    folder = lhm188.download(cfg, session=session)
    assert (folder / 'LibreHardwareMonitor.exe').exists() and json.loads((folder / lhm188.MARKER).read_text())['tag'] == 'v9'
    info = lhm188.check(cfg)
    assert info['installed'] and info['by_neo'] and not info['running']
    assert 'установлен' in lhm188.status_text(info, 'ru')
    lhm188.remove(cfg)
    assert not folder.exists()
    foreign = tmp_path / 'tools' / 'LibreHardwareMonitor'
    foreign.mkdir(parents=True)
    (foreign / 'LibreHardwareMonitor.exe').write_bytes(b'MZ')
    with pytest.raises(lhm188.LhmError) as e:
        lhm188.remove(cfg)
    assert e.value.key == 'not_ours' and foreign.exists()
