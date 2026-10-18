"""Real temp-directory scans, bounded OS inspection and production Agent routes."""
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time

import pytest

from jarvis import desktop_context as context
from jarvis.runtime import location_scan158 as scan
from jarvis.runtime.cancellation import cancellation_manager, OperationCancelled
from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from tools.atlas_all_test import check_phrase

ROOT=Path(__file__).resolve().parents[1]
CASES=json.loads((ROOT/'config/regression158_context.json').read_text(encoding='utf-8'))


@pytest.fixture(autouse=True)
def cancellation_generation():
    cancellation_manager().reset()
    yield
    cancellation_manager().reset()


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        instance=AtlasSession(tmp_path_factory.mktemp('locations158')/'run')
        prepare_fixtures(instance)
        yield instance
        instance.finish()


@pytest.mark.parametrize('case',CASES,ids=lambda r:r['locale']+':'+r['phrase'])
def test_real_agent_routes_local_game_and_app_paths(session,case):
    session.reset_case(case['locale'])
    result=check_phrase(session,case)
    assert result['status'] in {'ARGUMENTS_PASS','ROUTE_PASS'},result
    assert all(c['tool']=='game_locations' for c in result['calls']),result


@pytest.mark.parametrize('text,lang',[
    ('где находится моя папка','ru'),('найди файл report.txt','ru'),
    ('сколько уровней в игре Darktide','ru'),('не ищи файлы игры Darktide','ru'),
    ('where is London','en'),('find files for report','en'),
    ('how many levels are in Darktide','en'),('do not find files of the game Darktide','en'),
])
def test_location_scope_never_captures_web_questions_or_generic_files(text,lang):
    result=parse_feature_command(text,lang)
    assert result is None or result[0]!='game_locations'


def touch(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('fixture',encoding='utf-8')
    return path


def test_multiple_local_roots_find_install_related_exe_saves_videos_and_config(tmp_path):
    c=tmp_path/'C';d=tmp_path/'D'
    exe=touch(d/'Games/Darktide/binaries/Darktide.exe')
    touch(d/'Games/Darktide/installer.exe')
    save=c/'Users/Saved Games/Darktide';save.mkdir(parents=True)
    config=c/'Users/AppData/Roaming/Publisher/Darktide/config';config.mkdir(parents=True)
    video=d/'Recordings/Videos/Darktide';video.mkdir(parents=True)
    scanner=scan.LocationScanner()
    rows,status=scanner.search([c,d],{'darktide'},wait=.75)
    assert status['continuable'] is False
    assert {(r['kind'],r['path']) for r in rows}>={('exe',str(exe)),('saves',str(save)),('config',str(config)),('videos',str(video))}
    assert not any('installer.exe' in row['path'] for row in rows)
    assert all(Path(row['path']).is_absolute() and Path(row['path']).exists() for row in rows)
    assert all(row['confirmed'] is False for row in rows)


def test_deleted_cached_paths_are_never_returned(tmp_path):
    exe=touch(tmp_path/'Darktide/Darktide.exe')
    scanner=scan.LocationScanner()
    first,_=scanner.search([tmp_path],{'darktide'},wait=.75)
    assert any(r['path']==str(exe) for r in first)
    exe.unlink()
    second,_=scanner.search([tmp_path],{'darktide'},wait=.75)
    assert not any(r['path']==str(exe) for r in second)


def test_broad_scan_does_not_call_game_utilities_or_generic_runtime_the_game_exe(tmp_path):
    main=touch(tmp_path/'Games/Darktide/binaries/Darktide.exe')
    shipping=touch(tmp_path/'Games/Darktide/binaries/Darktide-Win64-Shipping.exe')
    for name in ['launcher/GPUDetection.exe', 'launcher/HashDatabaseCreator.exe',
                 'shader_cache/darktide_shader_cache_builder.exe', 'tools/dtkit-patch.exe',
                 'tools/javaw.exe', 'tools/game.exe', 'tools/renderer.exe']:
        touch(tmp_path/'Games/Darktide'/name)
    rows,status=scan.LocationScanner().search([tmp_path],{'darktide'},wait=.75)
    assert not status['continuable']
    assert {r['path'] for r in rows if r['kind']=='exe'}=={str(main),str(shipping)}


def test_symlink_loop_and_external_target_are_not_traversed(tmp_path):
    root=tmp_path/'scan';root.mkdir()
    outside=tmp_path/'private';touch(outside/'Darktide/saves/secret.dat')
    try:
        (root/'loop').symlink_to(root,target_is_directory=True)
        (root/'DarktideLink').symlink_to(outside,target_is_directory=True)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Windows account cannot create symlinks; reparse-point rejection has a separate test')
        raise
    scanner=scan.LocationScanner()
    rows,status=scanner.search([root],{'darktide'},wait=.75)
    assert rows==[] and status['continuable'] is False
    assert status['checked_entries']==0


def test_permission_denied_reports_incomplete_and_continues_other_directory(tmp_path,monkeypatch):
    blocked=tmp_path/'blocked';blocked.mkdir()
    expected=touch(tmp_path/'Darktide/Darktide.exe')
    original=scan.os.scandir
    def controlled(path):
        if Path(path)==blocked:raise PermissionError('fixture')
        return original(path)
    monkeypatch.setattr(scan.os,'scandir',controlled)
    rows,status=scan.LocationScanner().search([tmp_path],{'darktide'},wait=.75)
    assert status['incomplete'] and status['inaccessible']==1
    assert any(row['path']==str(expected) for row in rows)


def test_entry_budget_resumes_without_restart(tmp_path):
    for n in range(20):touch(tmp_path/f'Group{n}/Darktide/saves/a.dat')
    state=scan.Scan((str(tmp_path),),frozenset({'darktide'}))
    state.advance(seconds=1,entries=3)
    assert state.checked==3 and not state.complete
    for _ in range(40):
        state.advance(seconds=1,entries=3)
        if state.complete:break
    assert state.complete and len([r for r in state.rows.values() if r['kind']=='saves'])==20


def test_cancel_stops_scan_before_more_entries(tmp_path):
    touch(tmp_path/'Darktide/Darktide.exe')
    state=scan.Scan((str(tmp_path),),frozenset({'darktide'}))
    cancellation_manager().cancel('fixture cancelled')
    with pytest.raises(OperationCancelled):state.advance(seconds=1,entries=100)
    assert state.checked==0


def test_network_roots_are_excluded_even_if_os_says_they_exist(monkeypatch):
    config=SimpleNamespace(get=lambda key,default=None:[r'\\server\games','//server/games'] if key=='gaming.locations_scan_roots' else default)
    assert scan.scan_roots(config)==[]


def test_reparse_point_rejected_on_python_without_path_isjunction():
    entry=SimpleNamespace(stat=lambda **kw:SimpleNamespace(st_mode=0o040755,st_file_attributes=0x400))
    assert scan.is_link(Path('/junction'),entry)


def test_slow_open_files_is_bounded_and_only_one_worker_exists():
    blocker=threading.Event();entered=threading.Event();calls=[]
    def slow():
        calls.append(1);entered.set();blocker.wait(2);return []
    proc=SimpleNamespace(pid=1,create_time=lambda:10,exe=lambda:'C:/Game.exe',open_files=slow)
    snapshots=context._OpenFileSnapshots()
    start=time.monotonic()
    for _ in range(3):
        rows,_,pending=snapshots.read(proc,wait=.01)
        assert rows==[] and pending
    assert time.monotonic()-start<.25 and entered.is_set() and calls==[1]
    blocker.set()


def test_foreground_exact_title_file_and_process_identity_are_preserved(tmp_path,monkeypatch):
    document=touch(tmp_path/'notes.txt')
    exe=touch(tmp_path/'editor.exe')
    proc=SimpleNamespace(pid=8,create_time=lambda:10,exe=lambda:str(exe),open_files=lambda:[SimpleNamespace(path=str(document))])
    monkeypatch.setattr(context,'_open_file_snapshots',context._OpenFileSnapshots())
    monkeypatch.setattr(context,'_foreground_window',lambda:(22,8,'notepad.exe','notes.txt - Notepad',proc))
    result=context.inspect_foreground_context()
    assert result.file_path==str(document) and result.file_confidence==1
    assert result.process_exe==str(exe) and result.observed_at>0 and not result.files_pending


def test_pid_reuse_never_reuses_another_process_open_file(tmp_path):
    document=touch(tmp_path/'old.txt'); snapshots=context._OpenFileSnapshots()
    old=SimpleNamespace(pid=8,create_time=lambda:10,exe=lambda:'old.exe',open_files=lambda:[SimpleNamespace(path=str(document))])
    new=SimpleNamespace(pid=8,create_time=lambda:20,exe=lambda:'new.exe',open_files=lambda:[])
    assert snapshots.read(old,wait=.1)[0]
    rows,exe,pending=snapshots.read(new,wait=.1)
    assert rows==[] and exe=='new.exe' and not pending


def test_ambiguous_open_files_fail_closed(tmp_path,monkeypatch):
    a=touch(tmp_path/'a/notes.txt');b=touch(tmp_path/'b/notes.txt')
    proc=SimpleNamespace(pid=1,create_time=lambda:1,exe=lambda:'editor.exe',open_files=lambda:[SimpleNamespace(path=str(a)),SimpleNamespace(path=str(b))])
    monkeypatch.setattr(context,'_foreground_window',lambda:(1,1,'notepad.exe','notes.txt - Notepad',proc))
    monkeypatch.setattr(context,'_open_file_snapshots',context._OpenFileSnapshots())
    assert context.inspect_foreground_context().file_path==''


@pytest.fixture
def local_config(tmp_path,monkeypatch):
    import psutil
    from jarvis.runtime import game_locations as locations
    from jarvis.testing.atlas_session import AtlasConfig
    cfg=AtlasConfig(tmp_path/'profile','ru')
    cfg.data['gaming']['locations_scan_roots']=[str(tmp_path/'disk')]
    monkeypatch.setattr(locations,'_steam_roots',lambda:[])
    monkeypatch.setattr(locations,'_xbox_roots',lambda:[])
    monkeypatch.setattr(locations,'_registry_games',lambda deadline:iter(()))
    monkeypatch.setattr(locations,'_installed_apps',lambda deadline:iter(()))
    monkeypatch.setattr(locations,'known_folders',lambda key:[])
    monkeypatch.setattr(psutil,'process_iter',lambda attrs:iter(()))
    monkeypatch.setattr(scan,'_scanner',scan.LocationScanner())
    return cfg


def test_one_command_continues_slices_and_streams_paths_in_one_section(local_config,tmp_path,monkeypatch):
    from jarvis.runtime.briefing_stream import stream_to
    from jarvis.tools.desktop_features import register_desktop_features
    expected=touch(tmp_path/'disk/Publisher/Darktide/Darktide.exe')
    save=tmp_path/'disk/Users/Saved Games/Darktide';save.mkdir(parents=True)
    original=scan.Scan.advance;advances=[]
    def tiny_slice(self,**kwargs):
        advances.append(self.checked)
        return original(self,seconds=1,entries=2,generation=kwargs.get('generation'))
    monkeypatch.setattr(scan.Scan,'advance',tiny_slice)
    tools={};events=[]
    register_desktop_features(SimpleNamespace(config=local_config,register=lambda t:tools.setdefault(t.name,t)))
    with stream_to(events.append):
        result=tools['game_locations'].handler({'action':'find','name':'Darktide','kind':'all'})
    assert len(advances)>1 and len(set(advances))==len(advances)
    assert result.success and str(expected) in result.text and str(save) in result.text
    assert not result.data.status['continuable'] and not result.data.status['searching']
    assert {event['section'] for event in events}=={'locations.paths'}
    assert 'Ищу связанные локальные папки' in events[0]['text']
    assert events[-1]['text']==result.text and 'Ищу связанные локальные папки' not in result.text
    assert 'сводка' not in result.text.casefold()


def test_budget_exhaustion_is_explicit_and_does_not_claim_full_disk_coverage(local_config,monkeypatch):
    from jarvis.runtime.game_locations import find_game_locations
    local_config.data['gaming']['locations_scan_seconds']=.1
    def unfinished(*args,**kwargs):
        time.sleep(.025)
        return [],{'incomplete':True,'continuable':True,'searching':True}
    monkeypatch.setattr(scan,'search_locations',unfinished)
    started=time.monotonic()
    rows=find_game_locations(local_config,'Example')
    assert .09<time.monotonic()-started<.5
    assert rows.incomplete and rows.status['continuable'] and not rows.status['searching']


def test_cancelling_progress_stops_command_and_never_publishes_late_results(local_config):
    from jarvis.runtime.game_locations import find_game_locations
    events=[]
    def cancel(rows):
        events.append(rows)
        cancellation_manager().cancel('fixture')
    with pytest.raises(OperationCancelled):find_game_locations(local_config,'Example',on_progress=cancel)
    assert len(events)==1


def test_installed_application_registry_path_must_exist(local_config,tmp_path,monkeypatch):
    from jarvis.runtime import game_locations as locations
    exe=touch(tmp_path/'Discord/app-1/Discord.exe')
    monkeypatch.setattr(locations,'_installed_apps',lambda deadline:iter([
        {'name':'Discord','install':str(exe.parent),'exe':str(exe),'source':'app_paths_registry','evidence':'fixture'},
        {'name':'Discord','install':str(tmp_path/'nonexistent'),'source':'installation_registry','evidence':'fixture'},
    ]))
    rows=locations.find_game_locations(local_config,'Discord')
    assert {r['path'] for r in rows if r['confirmed']}=={str(exe),str(exe.parent)}
    assert all('nonexistent' not in r['path'] for r in rows)


def game_presence_fixture(monkeypatch):
    from jarvis.gaming import manager as gaming
    first=gaming.GameAdapter('first','First Game',('first.exe',))
    second=gaming.GameAdapter('second','Second Game',('second.exe',))
    processes=[(first,gaming.GameProcess(101,'first.exe','C:/first.exe',10.)),
               (second,gaming.GameProcess(202,'second.exe','D:/second.exe',20.))]
    manager=gaming.GameManager(enabled=True)
    manager.detect_all=lambda:processes
    foreground=[101]
    monkeypatch.setattr(gaming,'_foreground_pid',lambda:foreground[0])
    return gaming.GamePresenceService(manager),foreground,processes


def test_foreground_game_switch_overrides_fresh_cache_with_both_games_alive(monkeypatch):
    presence,fg,_=game_presence_fixture(monkeypatch)
    assert presence.snapshot(max_age_seconds=100).game_id=='first'
    fg[0]=202
    result=presence.snapshot(max_age_seconds=100)
    assert result.game_id=='second' and result.foreground_verified


def test_game_to_neo_preserves_exact_owned_game_then_unrelated_window_clears(monkeypatch):
    import os
    presence,fg,_=game_presence_fixture(monkeypatch)
    assert presence.snapshot().game_id=='first'
    fg[0]=os.getpid()
    assert presence.snapshot().game_id=='first'
    fg[0]=303
    assert presence.snapshot(preferred_pid=101,preferred_game_id='first') is None


def test_previous_game_pid_reuse_never_selects_new_process_as_old_game(monkeypatch):
    import os
    from jarvis.gaming import manager as gaming
    presence,fg,processes=game_presence_fixture(monkeypatch)
    assert presence.snapshot().game_id=='first'
    replacement=gaming.GameAdapter('replacement','Replacement',('replacement.exe',))
    processes[0]=(replacement,gaming.GameProcess(101,'replacement.exe','X:/replacement.exe',90.))
    fg[0]=os.getpid()
    assert presence.snapshot() is None


def test_shared_runtime_context_rejects_pid_reuse_and_unrelated_foreground(monkeypatch):
    import os
    import psutil
    from jarvis.gaming import manager as gaming, runtime
    created=[10.];foreground=[101]
    proc=SimpleNamespace(is_running=lambda:True,create_time=lambda:created[0])
    monkeypatch.setattr(psutil,'Process',lambda pid:proc)
    monkeypatch.setattr(gaming,'_foreground_pid',lambda:foreground[0])
    runtime.record_active_game(game_id='first',game_name='First',pid=101,session_started_at=10.)
    try:
        assert runtime.active_game_context()['game_id']=='first'
        foreground[0]=os.getpid()
        assert runtime.active_game_context()['game_id']=='first'
        foreground[0]=303
        assert runtime.active_game_context() is None
        foreground[0]=101;created[0]=99.
        assert runtime.active_game_context() is None
    finally:
        runtime.clear_active_game()
