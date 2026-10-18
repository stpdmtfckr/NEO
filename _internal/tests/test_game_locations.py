"""Launcher fixtures; never scans the real Windows registry or starts a game."""
from pathlib import Path
from types import SimpleNamespace
import json
import time

import pytest
import psutil

from jarvis.runtime import game_locations as locations


class Config:
    def __init__(self, root, values=None):
        self.values = values or {}
        self.paths = SimpleNamespace(screenshots=root / 'Screenshots')

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(locations, '_steam_roots', lambda: [])
    monkeypatch.setattr(locations, '_xbox_roots', lambda: [])
    monkeypatch.setattr(locations, '_registry_games', lambda deadline: iter(()))
    monkeypatch.setattr(locations, '_installed_apps', lambda deadline: iter(()))
    monkeypatch.setattr(locations, 'known_folders', lambda key: [tmp_path / key])
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter(()))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    # The broad scan added after these launcher fixtures must also stay inside
    # the fixture. In particular, never enumerate the user's Windows drives.
    return Config(tmp_path, {'gaming.locations_scan_roots': [str(tmp_path)]})


def file(path, data=''):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding='utf-8')
    return path


def test_darktide_native_alias_actual_publisher_config_exe_and_no_unrelated_tools(tmp_path,env):
    steam=tmp_path/'Steam'
    install=steam/'steamapps/common/Warhammer 40,000 DARKTIDE'
    exe=file(install/'binaries/Darktide.exe')
    file(install/'CheatEngine.exe')
    file(steam/'steamapps/appmanifest_1361210.acf','"AppState" { "appid" "1361210" "name" "Warhammer 40,000: Darktide" "installdir" "Warhammer 40,000 DARKTIDE" }')
    cfg=file(tmp_path/'Roaming/Fatshark/Darktide/user_settings.config')
    logs=tmp_path/'Roaming/Fatshark/Darktide/console_logs';logs.mkdir()
    ms_cfg=file(tmp_path/'Roaming/Fatshark/MicrosoftStore/Darktide/user_settings.config')
    (install/'mods').mkdir()
    env.values['gaming.launcher_roots.steam']=[str(steam)]
    rows=locations.find_game_locations(env,'Дарктайд')
    assert {r['path'] for r in rows if r['kind']=='exe'}=={str(exe)}
    assert {r['path'] for r in rows if r['kind']=='config'}=={str(cfg),str(ms_cfg)}
    assert all(r['exists'] for r in rows)
    assert not any('CheatEngine' in r['path'] for r in rows)
    assert not any(r['kind']=='saves' for r in rows)
    assert all(r['confirmed'] and r['evidence'].startswith('https://support.fatshark.se/') for r in rows if r['kind'] in {'exe','config'})


def test_darktide_named_trainer_manifest_not_game(tmp_path,env):
    manifest=tmp_path/'Epic'
    exe=file(tmp_path/'Tools/Darktide Trainer/CheatEngine.exe')
    file(manifest/'fake.item',json.dumps({'DisplayName':'Cheat Engine for Darktide','InstallLocation':str(exe.parent),'LaunchExecutable':exe.name}))
    env.values['gaming.launcher_roots.epic_manifests']=[str(manifest)]
    assert not locations.find_game_locations(env,'Darktide')


def test_epic_verified_executable_and_inferred_mods(tmp_path, env):
    root = tmp_path / 'Epic'
    install = tmp_path / 'Games' / 'Example'
    exe = file(install / 'Binaries' / 'Win64' / 'Example.exe')
    (install / 'mods').mkdir()
    manifest = file(root / 'a.item', json.dumps({'DisplayName': 'Example Game', 'InstallLocation': str(install), 'LaunchExecutable': 'Binaries/Win64/Example.exe'}))
    env.values['gaming.launcher_roots.epic_manifests'] = [str(root)]
    rows = locations.find_game_locations(env, 'Example Game')
    executable = next(r for r in rows if r['kind'] == 'exe')
    assert executable['path'] == str(exe)
    assert executable['confirmed'] and executable['evidence'] == str(manifest)
    assert next(r for r in rows if r['kind'] == 'mods')['confidence'] == 'inferred'


@pytest.mark.parametrize('relative', ['../outside.exe', r'..\outside.exe', '/outside.exe', r'C:\outside.exe'])
def test_epic_does_not_escape_declared_install(tmp_path, env, relative):
    root = tmp_path / 'Manifests'
    install = tmp_path / 'Game'
    install.mkdir()
    file(tmp_path / 'outside.exe')
    file(root / 'a.item', json.dumps({'DisplayName': 'Example', 'InstallLocation': str(install), 'LaunchExecutable': relative}))
    env.values['gaming.launcher_roots.epic_manifests'] = [str(root)]
    assert not any(r['kind'] == 'exe' for r in locations.find_game_locations(env, 'Example'))


def test_steam_secondary_library_appid_screenshots(tmp_path, env):
    steam = tmp_path / 'Steam'
    second = tmp_path / 'SecondLibrary'
    install = second / 'steamapps' / 'common' / 'Example'
    install.mkdir(parents=True)
    file(steam / 'steamapps' / 'libraryfolders.vdf', '"1" { "path" "' + str(second) + '" }')
    manifest = file(second / 'steamapps' / 'appmanifest_123.acf', '"AppState" { "appid" "123" "name" "Example Game" "installdir" "Example" }')
    screenshots = steam / 'userdata' / '111' / '760' / 'remote' / '123' / 'screenshots'
    screenshots.mkdir(parents=True)
    env.values['gaming.launcher_roots.steam'] = [str(steam)]
    rows = locations.find_game_locations(env, 'Example Game')
    assert any(r['path'] == str(install) and r['source'] == 'steam_manifest' for r in rows)
    assert any(r['path'] == str(screenshots) and r['evidence'] == str(manifest) for r in rows)
    assert not any(r['kind'] == 'exe' for r in rows)  # Steam appmanifest does not declare an exe.


def test_gog_manifest_primary_task_only(tmp_path, env):
    games = tmp_path / 'GOG Games'
    install = games / 'Example'
    main = file(install / 'game.exe')
    file(install / 'uninstall.exe')
    file(install / 'goggame-123.info', json.dumps({'name': 'Example Game', 'playTasks': [
        {'type': 'FileTask', 'isPrimary': True, 'path': 'game.exe'},
        {'type': 'FileTask', 'isPrimary': False, 'path': 'uninstall.exe'},
    ]}))
    env.values['gaming.launcher_roots.gog'] = [str(games)]
    rows = locations.find_game_locations(env, 'Example Game', 'exe')
    assert [r['path'] for r in rows] == [str(main)]


def test_gog_registry_records(tmp_path, env, monkeypatch):
    install = tmp_path / 'GOG'
    exe = file(install / 'main.exe')
    monkeypatch.setattr(locations, '_registry_games', lambda deadline: iter([
        {'gameName': 'Example Game', 'path': str(install), 'exe': str(exe), 'registry_id': '123'},
    ]))
    rows = locations.find_game_locations(env, 'Example Game')
    assert any(r['path'] == str(exe) and r['source'] == 'gog_registry' for r in rows)


def test_xbox_manifest_namespace_content_folder(tmp_path, env):
    xbox = tmp_path / 'XboxGames'
    content = xbox / 'Example' / 'Content'
    exe = file(content / 'Binaries' / 'Game.exe')
    file(content / 'Dev.exe')
    file(content / 'MicrosoftGame.config', '''<Game xmlns="urn:test" configVersion="1">
      <Identity Name="ExamplePublisher.Example"/>
      <ShellVisuals DefaultDisplayName="Example Game"/>
      <ExecutableList><Executable Name="Binaries/Game.exe" Id="Game"/><Executable Name="Dev.exe" IsDevOnly="true"/></ExecutableList>
    </Game>''')
    env.values['gaming.launcher_roots.xbox'] = [str(xbox)]
    rows = locations.find_game_locations(env, 'Example Game')
    assert [r['path'] for r in rows if r['kind'] == 'exe'] == [str(exe)]
    assert any(r['path'] == str(content) and r['source'] == 'xbox_manifest' for r in rows)


def test_xbox_entity_xml_rejected(tmp_path, env):
    xbox = tmp_path / 'XboxGames'
    file(xbox / 'Example' / 'MicrosoftGame.config', '<!DOCTYPE Game [<!ENTITY x "Example Game">]><Game><ShellVisuals DefaultDisplayName="&x;"/></Game>')
    env.values['gaming.launcher_roots.xbox'] = [str(xbox)]
    assert not locations.find_game_locations(env, 'Example Game')


def test_user_mapping_alias_and_existing_paths_only(tmp_path, env):
    saves = tmp_path / 'CustomSaves'
    saves.mkdir()
    env.values['gaming.locations'] = {'Example Game': {'aliases': ['EG'], 'saves': str(saves), 'config': str(tmp_path / 'missing')}}
    rows = locations.find_game_locations(env, 'EG')
    assert len(rows) == 1
    assert rows[0]['path'] == str(saves) and rows[0]['confirmed']


def test_named_data_folder_is_not_automatically_claimed_as_save(tmp_path, env):
    game = tmp_path / 'Documents' / 'My Games' / 'Example Game'
    game.mkdir(parents=True)
    (game / 'config').mkdir()
    rows = locations.find_game_locations(env, 'Example Game')
    assert not any(r['kind'] == 'saves' for r in rows)
    assert any(r['kind'] == 'config' and not r['confirmed'] for r in rows)


def test_videos_and_neo_screenshots_are_separate(tmp_path, env):
    video = tmp_path / 'Videos' / 'Example Game'
    video.mkdir(parents=True)
    shots = env.paths.screenshots / 'ExampleGame'
    shots.mkdir(parents=True)
    rows = locations.find_game_locations(env, 'Example Game')
    assert any(r['kind'] == 'videos' and r['path'] == str(video) for r in rows)
    assert any(r['kind'] == 'screenshots' and r['path'] == str(shots) for r in rows)


def test_running_process_parent_is_inferred(tmp_path, env, monkeypatch):
    exe = file(tmp_path / 'Game' / 'bin' / 'ExampleGame.exe')
    proc = SimpleNamespace(pid=123, info={'name': 'ExampleGame.exe', 'exe': str(exe)})
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter([proc]))
    rows = locations.find_game_locations(env, 'Example Game')
    assert next(r for r in rows if r['kind'] == 'exe')['confirmed']
    assert not next(r for r in rows if r['kind'] == 'install')['confirmed']


def test_existing_game_alias_connects_process_name(tmp_path, env, monkeypatch):
    exe = file(tmp_path / 'Warframe' / 'Warframe.x64.exe')
    proc = SimpleNamespace(pid=123, info={'name': 'Warframe.x64.exe', 'exe': str(exe)})
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter([proc]))
    rows = locations.find_game_locations(env, 'варфрейм', 'exe')
    assert [r['path'] for r in rows] == [str(exe)]


def test_generic_java_not_claimed_as_minecraft(tmp_path, env, monkeypatch):
    exe = file(tmp_path / 'Java' / 'javaw.exe')
    proc = SimpleNamespace(pid=123, info={'name': 'javaw.exe', 'exe': str(exe)})
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter([proc]))
    assert locations.find_game_locations(env, 'minecraft', 'exe') == []


def test_no_network_roots_without_opt_in(env):
    env.values['gaming.launcher_roots.steam'] = [r'\\server\Steam', '//server/Steam']
    assert locations._roots(env, 'steam', []) == []
    assert locations._path(r'\\server\share') is None


def test_bounds_reject_oversized_manifest(tmp_path):
    path = file(tmp_path / 'large.item', 'x' * 200)
    assert locations._read_text(path, 100) == ''
    assert list(locations._children(tmp_path, time.monotonic() - 1)) == []


def test_kind_alias_and_invalid_kind(tmp_path, env):
    config = tmp_path / 'Settings'
    config.mkdir()
    env.values['gaming.locations'] = {'Example': {'config': str(config)}}
    assert locations.find_game_locations(env, 'Example', 'settings')[0]['kind'] == 'config'
    with pytest.raises(ValueError):
        locations.find_game_locations(env, 'Example', 'execute')
