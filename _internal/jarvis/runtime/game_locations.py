"""Bounded, read-only game/application discovery. Existing paths and evidence only.

Manifests prove install/executable paths. Conventional data directory names are
marked inferred, never presented as proof of a save format. Broad name-only disk
search is performed in bounded, resumable slices by one background worker.
"""
from __future__ import annotations
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from jarvis.runtime.file_index import known_folders
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.runtime.file_scan162 import ScanPacer

KINDS = frozenset({'install', 'exe', 'saves', 'config', 'screenshots', 'videos', 'logs', 'mods', 'related'})
ALIASES = {'settings': 'config', 'save': 'saves', 'video': 'videos', 'screenshot': 'screenshots', 'mod': 'mods'}
CHILDREN = {
    'saves': ('saves', 'save', 'SaveGames', 'Saved/SaveGames'),
    'config': ('config', 'configs', 'settings', 'Saved/Config'),
    'screenshots': ('screenshots', 'ScreenShots', 'Saved/Screenshots'),
    'videos': ('videos', 'recordings', 'captures'), 'logs': ('logs', 'Saved/Logs'),
    'mods': ('mods', 'Mods'),
}
_DARKTIDE_ALIASES = {'darktide','дарктайд','дарктайде','вархаммердарктайд','warhammer40000darktide','warhammer40kdarktide'}
_FATSHARK_DATA_DOC = 'https://support.fatshark.se/hc/en-us/articles/360017633918--PC-How-to-Provide-a-Crash-Report-Console-Log-Launcher-Log-or-user-settings-config'
_FATSHARK_EXE_DOC = 'https://support.fatshark.se/hc/en-us/articles/7709665369501--PC-How-to-Resolve-Darktide-Launcher-Issues'


class LocationResults(list):
    """Preserve the established list API while reporting scan coverage honestly."""
    def __init__(self, rows=(), **status):
        super().__init__(rows)
        self.status = status
        self.incomplete = bool(status.get('incomplete'))


def _inside(child, parent):
    """True when child is strictly below parent (either path separator)."""
    c = os.path.normcase(str(child)).rstrip('\\/')
    p = os.path.normcase(str(parent)).rstrip('\\/')
    return len(c) > len(p) and c.startswith(p) and c[len(p)] in '\\/'


def _collapse(rows):
    """Keep one line per place: nested name-only matches fold into their folder.

    A mods folder found by name hides its scanned sub-folders (their names are
    listed on the parent as ``children``), and a name-only "related" folder that
    merely contains other results is dropped. Verified rows are never hidden.
    """
    rows = [dict(row) for row in rows]
    kept = []
    dropped = []
    for row in rows:
        if not row.get('confirmed'):
            same_kind_parent = any(o is not row and o['kind'] == row['kind'] and _inside(row['path'], o['path']) for o in rows)
            container = row['kind'] == 'related' and any(o is not row and _inside(o['path'], row['path']) for o in rows)
            if same_kind_parent or container:
                dropped.append(row)
                continue
        kept.append(row)
    for row in dropped:
        parent_dir = os.path.normcase(os.path.dirname(str(row['path']).rstrip('\\/')))
        for parent in kept:
            if parent['kind'] == row['kind'] and os.path.normcase(str(parent['path']).rstrip('\\/')) == parent_dir:
                parent.setdefault('children', []).append(os.path.basename(str(row['path']).rstrip('\\/')))
                break
    return kept


def _norm(value):
    return ''.join(c for c in str(value).casefold().replace('ё', 'е') if c.isalnum())


def _read_text(path, maximum=256_000):
    try:
        with path.open('rb') as handle:
            raw = handle.read(maximum + 1)
        return raw.decode('utf-8-sig', errors='replace') if len(raw) <= maximum else ''
    except OSError:
        return ''


def _children(path, deadline, maximum=3000):
    cancellation_manager().raise_if_cancelled()
    if time.monotonic() > deadline:
        return
    pace = ScanPacer(cancelled=cancellation_manager().raise_if_cancelled)
    try:
        with os.scandir(path) as entries:
            for count, entry in enumerate(entries):
                pace.checkpoint()
                cancellation_manager().raise_if_cancelled()
                if count >= maximum or time.monotonic() > deadline:
                    break
                candidate = Path(entry.path)
                from jarvis.runtime.location_scan158 import is_link
                if not is_link(candidate, entry):
                    yield candidate
    except OSError:
        return


def _path(raw, allow_network=False):
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        return None
    text = os.path.expandvars(str(raw)).strip()
    if not allow_network and (text.startswith('\\\\') or text.startswith('//')):
        return None
    candidate = Path(text).expanduser()
    return candidate if candidate.is_absolute() else None


def _within(root, raw):
    """Launcher-declared relative paths must remain inside the install root."""
    if not raw or '\x00' in str(raw):
        return None
    clean = str(raw).replace('\\', '/')
    part = Path(clean)
    if not part.parts or part.is_absolute() or re.match(r'^[A-Za-z]:', clean) or '..' in part.parts:
        return None
    try:
        result = (root / part).resolve(strict=False)
        result.relative_to(root.resolve(strict=False))
        return result
    except (ValueError, OSError, RuntimeError):
        return None


def _roots(config, key, defaults):
    values = config.get('gaming.launcher_roots.' + key, None)
    if values is None:
        values = defaults
    if isinstance(values, (str, Path)):
        values = [values]
    if not isinstance(values, list):
        return []
    allow = bool(config.get('gaming.locations_allow_network', False))
    return [p for raw in values if (p := _path(raw, allow)) is not None]


def _registry_games(deadline):
    """GOG's explicit installation records; no uninstall-tree exe guessing."""
    if os.name != 'nt':
        return
    pace = ScanPacer(cancelled=cancellation_manager().raise_if_cancelled)
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (getattr(winreg, 'KEY_WOW64_32KEY', 0), getattr(winreg, 'KEY_WOW64_64KEY', 0)):
                try:
                    with winreg.OpenKey(hive, r'Software\GOG.com\Games', 0, winreg.KEY_READ | view) as games:
                        for index in range(3000):
                            pace.checkpoint()
                            if time.monotonic() > deadline:
                                return
                            try:
                                game_id = winreg.EnumKey(games, index)
                            except OSError:
                                break
                            try:
                                with winreg.OpenKey(games, game_id) as game:
                                    values = {'registry_id': game_id}
                                    for field in ('gameName', 'path', 'exe'):
                                        try:
                                            values[field] = str(winreg.QueryValueEx(game, field)[0])
                                        except OSError:
                                            pass
                                    if values.get('gameName') and values.get('path'):
                                        yield values
                            except OSError:
                                continue
                except OSError:
                    continue
    except ImportError:
        return


def _steam_roots():
    roots = [Path(os.getenv('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Steam'] if os.name == 'nt' else []
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
                roots.insert(0, Path(winreg.QueryValueEx(key, 'SteamPath')[0]))
        except (OSError, ImportError):
            pass
    return list(dict.fromkeys(roots))


def _xbox_roots():
    if os.name != 'nt':
        return []
    import ctypes
    roots = []
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        drive = Path(letter + ':\\')
        if ctypes.windll.kernel32.GetDriveTypeW(str(drive)) == 3:
            if (drive / '.GamingRoot').is_file() or (drive / 'XboxGames').is_dir():
                roots.append(drive / 'XboxGames')
    return roots


def _registry_icon_executable(value):
    """Extract only a literal executable path from DisplayIcon, never a command.

    Windows accepts a quoted or unquoted icon path followed by an optional icon
    index. An icon resource can point at a DLL; it is not an application exe.
    """
    text = str(value or '').strip()
    if not text or any(c in text for c in ('\x00', '\r', '\n')):
        return ''
    match = re.fullmatch(r'"(?P<quoted>[^"\r\n]+\.exe)"(?:\s*,\s*-?\d+)?|(?P<plain>[^"\r\n]+\.exe)(?:\s*,\s*-?\d+)?', text, re.I)
    return (match['quoted'] or match['plain']).strip() if match else ''


def _application_roots():
    """Check likely install roots before scanning unrelated personal folders."""
    if os.name != 'nt':
        return []
    roots = [Path(os.environ[key]) for key in ('ProgramFiles', 'ProgramFiles(x86)') if os.getenv(key)]
    if os.getenv('LOCALAPPDATA'):
        roots.append(Path(os.environ['LOCALAPPDATA']) / 'Programs')
    return list(dict.fromkeys(roots))


def _installed_apps(deadline):
    """Read explicit local installation records, without evaluating commands."""
    if os.name != 'nt':
        return
    import winreg
    pace = ScanPacer(cancelled=cancellation_manager().raise_if_cancelled)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (getattr(winreg, 'KEY_WOW64_32KEY', 0), getattr(winreg, 'KEY_WOW64_64KEY', 0)):
            for branch in (r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                           r'Software\Microsoft\Windows\CurrentVersion\App Paths'):
                try:
                    with winreg.OpenKey(hive, branch, 0, winreg.KEY_READ | view) as root:
                        for index in range(3000):
                            pace.checkpoint()
                            cancellation_manager().raise_if_cancelled()
                            if time.monotonic() >= deadline:
                                return
                            try:
                                key_name = winreg.EnumKey(root, index)
                            except OSError:
                                break
                            try:
                                with winreg.OpenKey(root, key_name) as key:
                                    def value(field):
                                        try:
                                            return str(winreg.QueryValueEx(key, field)[0] or '')
                                        except OSError:
                                            return ''
                                    if branch.endswith('App Paths'):
                                        yield {'name': Path(key_name).stem, 'exe': value('').strip('"'),
                                               'source': 'app_paths_registry', 'evidence': branch + '\\' + key_name}
                                    else:
                                        yield {'name': value('DisplayName'), 'install': value('InstallLocation').strip('"'),
                                               'icon_exe': _registry_icon_executable(value('DisplayIcon')),
                                               'source': 'installation_registry', 'evidence': branch + '\\' + key_name}
                            except OSError:
                                continue
                except OSError:
                    continue


def find_game_locations(config, name: str, kind: str = 'all', *, on_progress=None) -> list[dict]:
    cancellation_manager().raise_if_cancelled()
    query = _norm(str(name).strip())
    if len(query) < 2:
        return []
    kind = ALIASES.get(kind, kind)
    if kind not in KINDS | {'all'}:
        raise ValueError('Unknown game location kind')
    deadline = time.monotonic() + max(.1, min(10., float(config.get('gaming.locations_timeout_seconds', 2.5) or 2.5)))
    allow_network = bool(config.get('gaming.locations_allow_network', False))
    results, installs = [], []
    titles, processes = {query}, set()
    darktide = query in _DARKTIDE_ALIASES
    if darktide:
        titles.update(_DARKTIDE_ALIASES)
        processes.add('darktide')
    # Reuse the existing process catalogue; avoid treating a generic Java runtime
    # or launcher as a game's executable. Exact aliases also support Russian names.
    try:
        from jarvis.gaming.manager import GameManager
        for adapter in GameManager._KNOWN:
            names = [adapter.display_name, adapter.game_id, *adapter.aliases]
            if query in {_norm(value) for value in names}:
                titles.update(_norm(value) for value in names)
                processes.update(_norm(Path(value).stem) for value in adapter.process_names
                                 if value.casefold() not in {'java.exe', 'javaw.exe', 'minecraftlauncher.exe'})
    except ImportError:
        pass

    def matches(title):
        normalized = _norm(title)
        # A tool carrying the game's name is not the game. In particular a
        # matching process/window name never proves a Cheat Engine association.
        # The exclusion does not apply when that utility is the requested app.
        if any(token in normalized and not any(token in term for term in titles)
               for token in ('cheatengine','trainer','modmanager','benchmark')):
            return False
        if darktide:
            return normalized in _DARKTIDE_ALIASES
        return bool(normalized and any(term == normalized or (len(term) >= 4 and term in normalized) for term in titles))

    def add(k, raw, source, confidence='verified', *, game='', evidence=''):
        if time.monotonic() > deadline or len(results) >= 100:
            return
        p = _path(raw, allow_network)
        if p is None:
            return
        try:
            is_file = p.is_file()
            if (k == 'exe' and not is_file) or (k not in {'exe','config','logs'} and not p.is_dir()) or (k in {'config','logs'} and not (is_file or p.is_dir())):
                return
        except OSError:
            return
        if k == 'install' and not any(root == p for root, _, _ in installs):
            installs.append((p, game or name, source))
        if k == 'exe':
            processes.add(_norm(p.stem))
        if game:
            titles.add(_norm(game))
        if kind not in {'all', k}:
            return
        row = {'kind': k, 'path': str(p), 'exists': True, 'source': source, 'confidence': confidence,
               'confirmed': confidence == 'verified', 'game': game or str(name), 'evidence': evidence or source,
               'path_type':'file' if is_file else 'directory'}
        if not any(r['kind'] == k and os.path.normcase(r['path']) == os.path.normcase(str(p)) for r in results):
            results.append(row)

    explicit = config.get('gaming.locations', {}) or {}
    if isinstance(explicit, dict):
        for game, paths in explicit.items():
            if not isinstance(paths, dict):
                continue
            aliases = paths.get('aliases', [])
            aliases = aliases if isinstance(aliases, list) else [aliases]
            if not any(_norm(x) == query for x in [game, *aliases]):
                continue
            titles.add(_norm(game))
            for k, values in paths.items():
                k = ALIASES.get(k, k)
                if k in KINDS:
                    for raw in values if isinstance(values, list) else [values]:
                        add(k, raw, 'user', game=game, evidence='explicit_user_mapping_and_exists')

    steam_roots = _roots(config, 'steam', _steam_roots())
    libraries = list(steam_roots)
    for steam in steam_roots:
        text = _read_text(steam / 'steamapps' / 'libraryfolders.vdf', 1_000_000)
        for raw in re.findall(r'"path"\s+"([^\"]+)"', text):
            p = _path(raw.replace('\\\\', '\\'), allow_network)
            if p is not None and p not in libraries:
                libraries.append(p)
    for library in libraries[:64]:
        folder = library / 'steamapps'
        for manifest in _children(folder, deadline):
            if not (manifest.name.startswith('appmanifest_') and manifest.suffix == '.acf'):
                continue
            values = dict(re.findall(r'"([^"\n]+)"\s+"([^"\n]*)"', _read_text(manifest, 64_000)))
            title = values.get('name', '')
            if not matches(title):
                continue
            install = _within(folder / 'common', values.get('installdir', ''))
            if install is not None:
                add('install', install, 'steam_manifest', game=title, evidence=str(manifest))
            appid = values.get('appid', '')
            if appid.isdigit():
                for steam in steam_roots:
                    for user in _children(steam / 'userdata', deadline, 100):
                        add('screenshots', user / '760' / 'remote' / appid / 'screenshots', 'steam_appid', game=title, evidence=str(manifest))
                        add('saves', user / appid / 'remote', 'steam_cloud_directory', 'inferred', game=title,
                            evidence='existing_appid_cloud_folder_may_also_contain_settings')

    epic_default = [Path(os.getenv('PROGRAMDATA', r'C:\ProgramData')) / 'Epic' / 'EpicGamesLauncher' / 'Data' / 'Manifests'] if os.name == 'nt' else []
    for folder in _roots(config, 'epic_manifests', epic_default):
        for manifest in _children(folder, deadline):
            if manifest.suffix.casefold() != '.item':
                continue
            try:
                data = json.loads(_read_text(manifest))
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict) or data.get('bIsIncompleteInstall') or not matches(data.get('DisplayName', '')):
                continue
            install = _path(data.get('InstallLocation'), allow_network)
            if install is None:
                continue
            title = data.get('DisplayName', name)
            add('install', install, 'epic_manifest', game=title, evidence=str(manifest))
            exe = _within(install, str(data.get('LaunchExecutable') or ''))
            if exe is not None and exe.suffix.casefold() == '.exe':
                add('exe', exe, 'epic_manifest', game=title, evidence=str(manifest))

    gog_default = [Path(os.getenv('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'GOG Galaxy' / 'Games', Path(r'C:\GOG Games')] if os.name == 'nt' else []
    gog_roots = _roots(config, 'gog', gog_default)
    for row in _registry_games(deadline):
        install = _path(row.get('path'), allow_network)
        if install is None or not matches(row['gameName']):
            continue
        evidence = 'GOG.com/Games/' + row['registry_id']
        add('install', install, 'gog_registry', game=row['gameName'], evidence=evidence)
        exe = _path(row.get('exe'), allow_network) or _within(install, row.get('exe', ''))
        if exe is not None and exe.suffix.casefold() == '.exe':
            try:
                exe.resolve().relative_to(install.resolve())
                add('exe', exe, 'gog_registry', game=row['gameName'], evidence=evidence)
            except (ValueError, OSError):
                pass
        gog_roots.append(install)
    for root in list(dict.fromkeys(gog_roots))[:64]:
        for folder in [root, *(p for p in _children(root, deadline, 500) if p.is_dir())]:
            for manifest in _children(folder, deadline, 300):
                if not (manifest.name.startswith('goggame-') and manifest.suffix == '.info'):
                    continue
                try:
                    data = json.loads(_read_text(manifest))
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict) or not matches(data.get('name', '')):
                    continue
                add('install', folder, 'gog_manifest', game=data['name'], evidence=str(manifest))
                for task in data.get('playTasks', []):
                    if not isinstance(task, dict) or not task.get('isPrimary') or task.get('type') != 'FileTask':
                        continue
                    exe = _within(folder, str(task.get('path') or ''))
                    if exe is not None and exe.suffix.casefold() == '.exe':
                        add('exe', exe, 'gog_manifest', game=data['name'], evidence=str(manifest))

    for root in _roots(config, 'xbox', _xbox_roots())[:64]:
        for folder in [root, *(p for p in _children(root, deadline, 500) if p.is_dir())]:
            for manifest in (folder / 'MicrosoftGame.config', folder / 'Content' / 'MicrosoftGame.config'):
                if time.monotonic() > deadline:
                    break
                raw = _read_text(manifest)
                if not raw or '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
                    continue
                try:
                    xml = ET.fromstring(raw)
                except ET.ParseError:
                    continue
                nodes = {node.tag.rsplit('}', 1)[-1]: node for node in xml.iter()}
                visuals = nodes.get('ShellVisuals')
                title = visuals.get('DefaultDisplayName', '') if visuals is not None else ''
                if title.startswith('ms-resource:') or not title:
                    title = folder.name
                if not matches(title):
                    continue
                install = manifest.parent
                add('install', install, 'xbox_manifest', game=title, evidence=str(manifest))
                for node in xml.iter():
                    if node.tag.rsplit('}', 1)[-1] != 'Executable' or node.get('IsDevOnly', '').casefold() == 'true':
                        continue
                    exe = _within(install, node.get('Name', ''))
                    if exe is not None and exe.suffix.casefold() == '.exe':
                        add('exe', exe, 'xbox_manifest', game=title, evidence=str(manifest))

    for app in _installed_apps(deadline):
        if matches(app.get('name', '')):
            for key in ('install', 'exe'):
                if app.get(key):
                    add(key, app[key], app['source'], game=app['name'], evidence=app['evidence'])
            # Many desktop installers (including Inno Setup) omit
            # InstallLocation but declare their executable in DisplayIcon.
            # Keep that evidence labelled inferred: an icon is an association,
            # not an instruction to launch it. Never return an uninstaller as
            # the application's executable.
            icon = _path(app.get('icon_exe'), allow_network)
            if icon is not None and icon.suffix.casefold() == '.exe':
                basename = _norm(icon.stem)
                helpers = ('uninstall', 'unins', 'installer', 'updater', 'setup')
                if not any(token in basename and token not in query for token in helpers):
                    add('exe', icon, 'installation_registry_icon', 'inferred',
                        game=app['name'], evidence=app['evidence'] + '/DisplayIcon')
                    if icon.is_file():
                        add('install', icon.parent, 'installation_registry_icon_parent', 'inferred',
                            game=app['name'], evidence=app['evidence'] + '/DisplayIcon')

    if time.monotonic() < deadline:
        try:
            import psutil
            for proc in psutil.process_iter(['name', 'exe']):
                if time.monotonic() > deadline:
                    break
                try:
                    exe = _path(proc.info.get('exe'), allow_network)
                    procname = _norm(Path(str(proc.info.get('name') or '')).stem)
                    if exe is not None and exe.suffix.casefold() == '.exe' and procname in titles | processes:
                        add('exe', exe, 'running_process', evidence='pid=' + str(proc.pid))
                        add('install', exe.parent, 'running_process_parent', 'inferred', evidence='pid=' + str(proc.pid))
                except (OSError, psutil.Error):
                    continue
        except (ImportError, OSError):
            pass

    data_roots = [*known_folders('Saved Games'), *known_folders('Documents')]
    if not any(p.name == 'Saved Games' for p in data_roots):
        data_roots.append(Path.home()/'Saved Games')
    if darktide and os.getenv('APPDATA'):
        for base in (Path(os.environ['APPDATA'])/'Fatshark'/'Darktide',
                     Path(os.environ['APPDATA'])/'Fatshark'/'MicrosoftStore'/'Darktide'):
            add('config',base/'user_settings.config','fatshark_documented_existing',game='Warhammer 40,000: Darktide',evidence=_FATSHARK_DATA_DOC)
            add('logs',base/'console_logs','fatshark_documented_existing',game='Warhammer 40,000: Darktide',evidence=_FATSHARK_DATA_DOC)
            add('logs',base/'darktide_launcher.log','fatshark_documented_existing',game='Warhammer 40,000: Darktide',evidence=_FATSHARK_DATA_DOC)
    for env in ('LOCALAPPDATA', 'APPDATA'):
        if os.getenv(env):
            data_roots.append(Path(os.environ[env]))
    for root in data_roots:
        for parent in (root, root / 'My Games'):
            for folder in _children(parent, deadline, 1500):
                if _norm(folder.name) not in titles or not folder.is_dir():
                    continue
                for k, children in CHILDREN.items():
                    for child in children:
                        add(k, folder / child, 'existing_game_data_directory', 'inferred', evidence=str(folder))
                if root.name == 'Saved Games':
                    add('saves', folder, 'saved_games_directory', 'inferred', evidence='matching_game_name_and_exists')
    for install, game, source in installs:
        if darktide and source != 'running_process_parent':
            add('exe',install/'binaries'/'Darktide.exe','fatshark_documented_existing',game=game,evidence=_FATSHARK_EXE_DOC)
        for k, children in CHILDREN.items():
            for child in children:
                add(k, install / child, source + '_data_directory', 'inferred', game=game, evidence='existing_named_child')
    for root in known_folders('Videos'):
        for folder in _children(root, deadline, 500):
            if _norm(folder.name) in titles:
                add('videos', folder, 'known_videos_game_directory', 'inferred', evidence='matching_game_name_and_exists')
    for folder in _children(Path(config.paths.screenshots), deadline, 500):
        if _norm(folder.name) in titles | processes:
            add('screenshots', folder, 'neo_screenshots', evidence='matching_application_folder_and_exists')
    status = {'incomplete': time.monotonic() >= deadline, 'continuable': False}
    source_incomplete = status['incomplete']
    if bool(config.get('gaming.locations_scan_enabled', True)):
        from jarvis.runtime.location_scan158 import search_locations, existing_rows
        priority=[*(root for root, _, _ in installs), *_application_roots(),
                  *data_roots, *known_folders('Videos'), *known_folders('Pictures')]
        budget=max(.1,min(30.,float(config.get('gaming.locations_scan_seconds',8.) or 8.)))
        scan_deadline=time.monotonic()+budget
        # Manifest/documented evidence takes precedence over a name-only match.
        seen = {(row['kind'], os.path.normcase(row['path'])) for row in results}
        last_count=-1
        next_progress=0.
        first_snapshot=True
        if callable(on_progress):
            on_progress(LocationResults(_collapse(results), searching=True, incomplete=True))
        while time.monotonic()<scan_deadline:
            cancellation_manager().raise_if_cancelled()
            broad, scan_status = search_locations(config,titles | processes,priority=priority,
                wait=min(.1,max(0.,scan_deadline-time.monotonic())), validate=first_snapshot)
            first_snapshot=False
            scan_status['incomplete'] = bool(scan_status.get('incomplete') or source_incomplete)
            status.update(scan_status)
            for row in broad:
                key = (row['kind'], os.path.normcase(row['path']))
                if key not in seen and kind in {'all', row['kind']}:
                    results.append({**row, 'game': str(name)})
                    seen.add(key)
            if callable(on_progress) and len(results)!=last_count and time.monotonic()>=next_progress:
                on_progress(LocationResults(_collapse(results),**{**status,'searching':True}))
                last_count=len(results)
                next_progress=time.monotonic()+.2
            if not status.get('continuable'):
                break
        status['searching']=False
        results=existing_rows(results)
        if callable(on_progress):
            on_progress(LocationResults(_collapse(results),**status))
    return LocationResults(_collapse(results), **status)
