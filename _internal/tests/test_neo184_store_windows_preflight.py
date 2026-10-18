"""Inert Windows API/path fixtures: no native API, real path or process access."""
from collections import Counter
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from jarvis.gaming import store_metadata as metadata

EXE = r"C:\Games\Steam\steamapps\common\Sparta\Binaries\game.exe"
LIBRARY = r"C:\Games\Steam\steamapps"
MANIFEST = LIBRARY + r"\appmanifest_123.acf"
DIR = 0x10
FILE = 0x80
REPARSE = 0x400


@pytest.fixture
def windows(monkeypatch):
    state = SimpleNamespace(drive_type=3, attributes={}, api=[], io=[], permit_stat=False)

    def key(raw):
        return str(PureWindowsPath(str(raw))).casefold()

    class InertPath:
        def __init__(self, raw):
            self.raw = str(PureWindowsPath(str(raw)))

        def __str__(self):
            return self.raw

        def __hash__(self):
            return hash(key(self.raw))

        def __eq__(self, other):
            return isinstance(other, InertPath) and key(self.raw) == key(other.raw)

        @property
        def name(self):
            return PureWindowsPath(self.raw).name

        def resolve(self, *args, **kwargs):
            state.io.append(("resolve", self.raw))
            raise AssertionError("Windows resolver must not follow a path")

        def open(self, *args, **kwargs):
            state.io.append(("open", self.raw))
            raise AssertionError("Native file open forbidden")

        def stat(self, *, follow_symlinks):
            state.io.append(("stat", self.raw))
            assert state.permit_stat and follow_symlinks is False
            is_file = self.name.endswith(".acf")
            return SimpleNamespace(st_dev=1, st_ino=12 if is_file else 11,
                                   st_size=128, st_mtime_ns=5, st_ctime_ns=6,
                                   st_file_attributes=FILE if is_file else DIR)

    def drive_type(root):
        state.api.append(("drive", root))
        if isinstance(state.drive_type, Exception):
            raise state.drive_type
        return state.drive_type

    def attributes(raw):
        state.api.append(("attributes", raw))
        value = state.attributes.get(key(raw), FILE if raw.endswith(("game.exe", ".acf")) else DIR)
        if isinstance(value, Exception):
            raise value
        return value

    def forbidden_scan(path):
        state.io.append(("scandir", str(path)))
        raise AssertionError("Native directory enumeration forbidden")

    def forbidden_api():
        raise AssertionError("Native Windows API forbidden")

    monkeypatch.setattr(metadata, "_WINDOWS", True)
    monkeypatch.setattr(metadata, "Path", InertPath)
    monkeypatch.setattr(metadata, "_windows_functions", forbidden_api)
    monkeypatch.setattr(metadata, "_windows_drive_type", drive_type)
    monkeypatch.setattr(metadata, "_windows_attributes", attributes)
    monkeypatch.setattr(metadata.os, "scandir", forbidden_scan)
    state.path = InertPath
    state.key = key
    state.resolver = metadata.SteamTitleResolver(clock=lambda: 10.0)
    return state


@pytest.mark.parametrize("raw", [
    r"\\server\share\steamapps\common\Sparta\game.exe",
    "//server/share/steamapps/common/Sparta/game.exe",
    r"\/server/share/steamapps/common/Sparta/game.exe",
    r"/\server\share\steamapps\common\Sparta\game.exe",
    r"\\?\C:\Games\steamapps\common\Sparta\game.exe",
    r"\/?/C:/Games/steamapps/common/Sparta/game.exe",
    r"/\.\C:\Games\steamapps\common\Sparta\game.exe",
    r"\\.\GLOBALROOT\Device\HarddiskVolume1\steamapps\common\Sparta\game.exe",
    r"C:Games\steamapps\common\Sparta\game.exe",
    r"\Games\steamapps\common\Sparta\game.exe",
    "Games/steamapps/common/Sparta/game.exe",
    r"C:\Games\steamapps\common\Sparta\..\Other\game.exe",
    r"C:\Games\steamapps\common\.\Sparta\game.exe",
    r"C:\Games\steamapps\common\Sparta\game.exe:payload",
    r"C:\Games\steamapps\common\Sparta\NUL",
    r"C:\Games\steamapps\common\Sparta\CON.txt",
    r"C:\Games\steamapps\common\Sparta\COM1.exe",
    r"C:\Games\steamapps\common\Sparta\CONIN$",
    "C:\\Games\\steamapps\\common\\Sparta\\game.exe\x00",
    "C:\\Games\\steamapps\\common\\Sparta\\game\n.exe",
    r"C:\Games\steamapps\common\Sparta.\game.exe",
    r"C:\Games\steamapps\common\Sparta \game.exe",
    "C:\\Games\\steamapps\\common\\Sparta\\\\game.exe",
])
def test_unsafe_lexical_paths_never_reach_api_or_filesystem(windows, raw):
    title = windows.resolver.title_for(raw)
    assert not title.name and title.status == "unsupported_install_path"
    assert windows.api == []
    assert windows.io == []  # zero resolve/stat/open/scandir


@pytest.mark.parametrize("drive_type", [0, 1, 4, 7, -1, None, True, "3", OSError("unavailable"), RuntimeError("unknown")])
def test_mapped_remote_or_unknown_drive_rejected_before_attributes_or_io(windows, drive_type):
    windows.drive_type = drive_type
    title = windows.resolver.title_for(EXE)
    assert not title.name and title.status == "unverified_install_root"
    assert windows.api == [("drive", "C:\\")]
    assert windows.io == []


@pytest.mark.parametrize("parent", ["C:\\", r"C:\Games", r"C:\Games\Steam", LIBRARY,
                                    LIBRARY + r"\common", LIBRARY + r"\common\Sparta",
                                    LIBRARY + r"\common\Sparta\Binaries", EXE])
def test_junction_or_reparse_component_rejected_without_querying_below_it(windows, parent):
    windows.attributes[windows.key(parent)] = REPARSE | (FILE if parent == EXE else DIR)
    title = windows.resolver.title_for(EXE)
    assert not title.name and title.status == "unverified_install_root"
    assert windows.api[-1] == ("attributes", parent)
    assert windows.io == []


@pytest.mark.parametrize("attributes", [None, 0, -1, 0xFFFFFFFF, 0x100000000, True, "16", OSError("unknown"), RuntimeError("unknown"),
                                       DIR | 0x40, DIR | 0x1000, DIR | 0x40000, DIR | 0x400000])
def test_unknown_device_offline_or_recall_attributes_fail_closed(windows, attributes):
    windows.attributes[windows.key(r"C:\Games")] = attributes
    assert windows.resolver.title_for(EXE).status == "unverified_install_root"
    assert windows.api[-1] == ("attributes", r"C:\Games")
    assert windows.io == []


@pytest.mark.parametrize("drive_type", [2, 3, 5, 6])
def test_known_local_drive_types_allow_only_inert_preflight(windows, drive_type):
    windows.drive_type = drive_type
    assert metadata._windows_preflight(EXE, directory=False)
    assert windows.api[0] == ("drive", "C:\\")
    assert windows.api[-1] == ("attributes", EXE)
    assert windows.io == []


def test_drive_absolute_forward_slashes_are_normalized_without_traversal(windows):
    assert metadata._windows_preflight(EXE.replace("\\", "/"), directory=False)
    assert windows.api[-1] == ("attributes", EXE)
    assert windows.io == []


@pytest.mark.parametrize("target,attributes", [(r"C:\Games", FILE), (EXE, DIR)])
def test_parent_must_be_directory_and_executable_must_not_be_directory(windows, target, attributes):
    windows.attributes[windows.key(target)] = attributes
    assert not metadata._windows_preflight(EXE, directory=False)
    assert windows.io == []


def prime_cache(windows):
    windows.permit_stat = True
    title = metadata.StoreTitle("Fixture Product", "steam_manifest", "verified", "123")
    windows.resolver._cache[windows.path(LIBRARY)] = metadata._Inventory(
        10.0, (1, 11, 128, 5, 6),
        {metadata.os.path.normcase("Sparta"): (title, windows.path(MANIFEST), (1, 12, 128, 5, 6))}, "metadata_missing")
    return title


def test_cached_safe_path_uses_no_resolve_open_or_rescan(windows):
    title = prime_cache(windows)
    assert windows.resolver.title_for(EXE) == title
    assert Counter(call[0] for call in windows.io) == {"stat": 2}


@pytest.mark.parametrize("mutation", ["remote", "junction", "unknown"])
def test_cache_never_bypasses_current_drive_parent_preflight(windows, mutation):
    prime_cache(windows)
    if mutation == "remote":
        windows.drive_type = 4
    elif mutation == "junction":
        windows.attributes[windows.key(r"C:\Games")] = DIR | REPARSE
    else:
        windows.drive_type = None
    assert windows.resolver.title_for(EXE).status == "unverified_install_root"
    assert windows.io == []


def test_cached_manifest_reparse_is_rejected_before_stat_open_or_scandir(windows):
    prime_cache(windows)
    windows.attributes[windows.key(MANIFEST)] = FILE | REPARSE
    assert windows.resolver.title_for(EXE).status == "metadata_changed"
    assert windows.io == [("stat", LIBRARY)]
    assert not any(operation in {"resolve", "open", "scandir"} for operation, path in windows.io)


def test_manifest_signature_rechecks_parents_before_any_stat(windows):
    windows.attributes[windows.key(r"C:\Games\Steam")] = DIR | REPARSE
    with pytest.raises(OSError, match="Unverified local metadata path"):
        metadata._signature(windows.path(MANIFEST))
    assert windows.io == []


def test_manifest_discovered_in_safe_library_is_checked_before_leaf_operations(windows, monkeypatch):
    windows.attributes[windows.key(MANIFEST)] = FILE | REPARSE

    class Entry:
        name = "appmanifest_123.acf"
        path = MANIFEST

        def is_symlink(self):
            raise AssertionError("Reparse entry must be rejected before leaf stat")

        def is_file(self, **kwargs):
            raise AssertionError("Reparse entry must be rejected before leaf stat")

    class Entries:
        def __enter__(self):
            return iter([Entry()])

        def __exit__(self, *args):
            return False

    def scan(path):
        windows.io.append(("scandir", str(path)))
        return Entries()

    monkeypatch.setattr(metadata.os, "scandir", scan)
    inventory = windows.resolver._scan(windows.path(LIBRARY), None, 10.0)
    assert inventory.status == "invalid_manifest" and not inventory.rows
    # One inert, preflight-approved parent enumeration discovers the manifest;
    # there is no resolve, manifest stat, file open or child enumeration.
    assert windows.io == [("scandir", LIBRARY)]


def test_scan_rejects_new_unsafe_parent_before_enumeration(windows):
    windows.attributes[windows.key(LIBRARY)] = DIR | REPARSE
    assert windows.resolver._scan(windows.path(LIBRARY), None, 10.0).status == "metadata_unavailable"
    assert windows.io == []
