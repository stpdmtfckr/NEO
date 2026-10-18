"""Synthetic store fixtures only; no process enumeration or launcher discovery."""
from __future__ import annotations

import os
import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.gaming import store_metadata as metadata
from jarvis.gaming.manager import GameAdapter, GameManager, GamePresenceService, GameProcess


def manifest(app_id="123", name="Fixture Product II", folder="Sparta"):
    return f'"AppState" {{ "appid" "{app_id}" "name" "{name}" "installdir" "{folder}" }}'.encode()


def write(path, data=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def install(tmp_path, monkeypatch):
    clock = [10.0]
    resolver = metadata.SteamTitleResolver(clock=lambda: clock[0])
    library = tmp_path / "FixtureLibrary" / "steamapps"
    exe = write(library / "common" / "Sparta" / "Binaries" / "game.exe")
    monkeypatch.setattr(metadata, "_RESOLVER", resolver)
    return library, exe, resolver, clock


@pytest.fixture(autouse=True)
def block_process_enumeration(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Native process enumeration is forbidden")
    monkeypatch.setattr("psutil.process_iter", forbidden)
    monkeypatch.setattr("jarvis.gaming.manager._foreground_pid", lambda: None)


def test_exact_manifest_title_keeps_session_identity_and_directory_alias(install):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest())
    process = GameProcess(812, "game.exe", str(exe), 123.5, ("private-argument",))
    adapter = GameManager._store_adapter(process)
    assert adapter.display_name == "Fixture Product II"
    assert adapter.game_id == "store_sparta"
    assert adapter.aliases == ("Sparta",)
    assert adapter.process_names == ("game.exe",)
    with patch("jarvis.gaming.manager.time.time", return_value=200):
        snap = adapter.snapshot(process)
    assert (snap.pid, snap.session_started_at, snap.session_seconds) == (812, 123.5, 76)
    assert snap.executable == str(exe)
    assert snap.capabilities == ["session"]
    assert not snap.foreground_verified
    diagnostic = {k: v for k, v in snap.as_dict().items() if k in {
        "game_name_source", "game_name_status", "game_store_app_id"}}
    assert diagnostic == {"game_name_source": "steam_manifest", "game_name_status": "verified", "game_store_app_id": "123"}
    assert all(str(library) not in value and "private-argument" not in value for value in diagnostic.values())


def test_missing_metadata_visibly_labels_directory_without_guessing(install):
    library, exe, resolver, clock = install
    adapter = GameManager._store_adapter(GameProcess(1, "game.exe", str(exe), 1))
    assert adapter.display_name == "Game folder: Sparta"
    assert adapter.game_name_source == "installation_directory"
    assert adapter.game_name_status == "metadata_missing"
    assert adapter.game_id == "store_sparta"
    assert not adapter.game_store_app_id


def test_no_hardcoded_sparta_product_mapping(install):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest(name="An Unrelated Fixture"))
    assert resolver.title_for(str(exe)).name == "An Unrelated Fixture"


@pytest.mark.parametrize("raw", [
    b'"AppState" { "appid" "123" "name" "A" "installdir" "Sparta"',
    b'"AppState" { "appid" "123" "name" "A" "installdir" "Sparta" }}',
    b'garbage "AppState" { "appid" "123" "name" "A" "installdir" "Sparta" }',
    b'"NotAppState" { "appid" "123" "name" "A" "installdir" "Sparta" }',
    b'"AppState" "value"',
    b'"AppState" { "appid" "123" "name" { "value" "A" } "installdir" "Sparta" }',
    b'"AppState" { "appid" "123" "name" "A" "installdir" "Sparta" "name" "B" }',
    b'"AppState" { "appid" "123" "name" "A" "installdir" "Sparta" "NAME" "B" }',
    b'"AppState" { "appid" "123" "name" "A" "installdir" "Sparta" } "AppState" {}',
    b'"AppState" { "appid" "123" "name" "A" "installdir" "Sparta" } "Other" {}',
    b'"AppState" { "appid" "123" "nested" { "name" "A" "installdir" "Sparta" } }',
    b'"AppState" { "appid" "123" "name" "\xff" "installdir" "Sparta" }',
    manifest(name=""), manifest(name="   "), manifest(name=" A"), manifest(name="A\nB"),
    manifest(name="A\x00B"), manifest(name="A\u202eB"), manifest(name="A\u2028B"), manifest(name="A" * 257),
    manifest(folder=""), manifest(folder=".."), manifest(folder="../Sparta"),
    manifest(folder=r"..\Sparta"), manifest(folder="/Sparta"), manifest(folder="C:Sparta"),
    manifest(folder="Sparta "), manifest(app_id="999"),
])
def test_malformed_or_ambiguous_fields_rejected(raw):
    assert metadata.parse_steam_manifest(raw, "appmanifest_123.acf") is None


def test_nested_non_title_keys_comments_bom_and_escaped_quote():
    raw = b'\xef\xbb\xbf// fixture\n"AppState" {"appid" "123" "name" "A \\"Quote\\"" "installdir" "Sparta" "nested" {"name" "Ignore"}}'
    assert metadata.parse_steam_manifest(raw, "appmanifest_123.acf") == ("123", 'A "Quote"', "Sparta")


@pytest.mark.parametrize("filename", ["appmanifest_999.acf", "appmanifest_x.acf", "appmanifest_0123.acf", "other.acf"])
def test_filename_appid_consistency(filename):
    assert metadata.parse_steam_manifest(manifest(), filename) is None


def test_oversized_manifest_rejected_before_read(install, monkeypatch):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest() + b" " * metadata.MAX_MANIFEST_BYTES)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Oversized file opened"))
    assert resolver.title_for(str(exe)).status == "oversized_manifest"


@pytest.mark.parametrize("second_name", ["Fixture Product II", "Conflicting Product"])
def test_multiple_manifest_matches_fail_closed_even_same_product(install, second_name):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest())
    write(library / "appmanifest_456.acf", manifest("456", second_name))
    title = resolver.title_for(str(exe))
    assert not title.name and not title.app_id
    assert title.status == "ambiguous_manifest"


@pytest.mark.parametrize("invalid", [
    manifest("456", "", "Other"), manifest("999", "Other Product", "Other"),
    b'"AppState" { "appid" "456" "name" "A" "name" "B" "installdir" "Other" }',
])
def test_invalid_product_metadata_for_proven_other_install_does_not_erase_title(install, invalid):
    library, exe, resolver, clock = install
    other_exe = write(library / "common" / "Other" / "other.exe")
    write(library / "appmanifest_123.acf", manifest())
    write(library / "appmanifest_456.acf", invalid)
    assert resolver.title_for(str(exe)).name == "Fixture Product II"
    assert resolver.title_for(str(other_exe)).status == "invalid_manifest"


@pytest.mark.parametrize("invalid", [
    b'"AppState" { "appid" "456" "name" "A" "installdir" "Other"',
    b'"AppState" { "appid" "456" "name" "A" "installdir" "Other" "installdir" "Sparta" }',
    b'"AppState" { "appid" "456" "name" "A" "installdir" "Other" } "AppState" {}',
])
def test_unparseable_or_ambiguous_install_ownership_cannot_claim_unrelated(install, invalid):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest())
    write(library / "appmanifest_456.acf", invalid)
    title = resolver.title_for(str(exe))
    assert not title.name and title.status == "invalid_manifest"


def test_invalid_title_for_same_install_blocks_otherwise_valid_duplicate(install):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest())
    write(library / "appmanifest_456.acf", manifest("456", "", "Sparta"))
    assert resolver.title_for(str(exe)).status == "ambiguous_manifest"


def test_metadata_from_another_library_never_names_same_folder(install, tmp_path):
    library, exe, resolver, clock = install
    write(tmp_path / "WrongLibrary" / "steamapps" / "appmanifest_123.acf", manifest())
    assert resolver.title_for(str(exe)).status == "metadata_missing"


def test_wrong_install_directory_does_not_match_substring(install):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest(folder="Sparta Demo"))
    assert not resolver.title_for(str(exe)).name


def test_renamed_install_directory_requires_updated_manifest(install):
    library, exe, resolver, clock = install
    manifest_path = write(library / "appmanifest_123.acf", manifest())
    assert resolver.title_for(str(exe)).name
    renamed_exe = write(library / "common" / "Renamed" / "game.exe")
    assert not resolver.title_for(str(renamed_exe)).name
    write(manifest_path, manifest(folder="Renamed"))
    clock[0] += metadata.CACHE_SECONDS + 1
    assert resolver.title_for(str(renamed_exe)).name == "Fixture Product II"


def test_selected_file_edit_invalidates_before_rescan_and_updates_after_throttle(install):
    library, exe, resolver, clock = install
    target = write(library / "appmanifest_123.acf", manifest())
    assert resolver.title_for(str(exe)).name == "Fixture Product II"
    write(target, manifest(name="Updated Fixture Title"))
    assert resolver.title_for(str(exe)).status == "metadata_changed"
    clock[0] += metadata.MIN_SCAN_SECONDS
    assert resolver.title_for(str(exe)).name == "Updated Fixture Title"


@pytest.mark.parametrize("action", ["remove", "rename", "duplicate"])
def test_directory_changes_invalidate_cached_evidence(install, action):
    library, exe, resolver, clock = install
    target = write(library / "appmanifest_123.acf", manifest())
    assert resolver.title_for(str(exe)).name
    if action == "remove":
        target.unlink()
    elif action == "rename":
        target.rename(library / "appmanifest_999.acf")
    else:
        write(library / "appmanifest_456.acf", manifest("456", "Other Product"))
    assert not resolver.title_for(str(exe)).name
    clock[0] += metadata.MIN_SCAN_SECONDS
    assert not resolver.title_for(str(exe)).name


def test_negative_cache_detects_new_manifest(install):
    library, exe, resolver, clock = install
    assert not resolver.title_for(str(exe)).name
    write(library / "appmanifest_123.acf", manifest())
    assert not resolver.title_for(str(exe)).name  # throttled, never stale guessed title
    clock[0] += metadata.MIN_SCAN_SECONDS
    assert resolver.title_for(str(exe)).name


def test_polling_and_other_processes_in_library_reuse_inventory(install, monkeypatch):
    library, exe, resolver, clock = install
    other_exe = write(library / "common" / "Other" / "other.exe")
    write(library / "appmanifest_123.acf", manifest())
    write(library / "appmanifest_456.acf", manifest("456", "Other Product", "Other"))
    assert resolver.title_for(str(exe)).name
    monkeypatch.setattr(metadata.os, "scandir", lambda *args: pytest.fail("Repeated inventory scan"))
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Repeated manifest read"))
    for _ in range(30):
        assert resolver.title_for(str(exe)).name == "Fixture Product II"
        assert resolver.title_for(str(other_exe)).name == "Other Product"


def test_in_place_other_manifest_change_is_found_at_bounded_ttl(install):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest())
    other = write(library / "appmanifest_456.acf", manifest("456", "Other", "Other"))
    assert resolver.title_for(str(exe)).name
    write(other, manifest("456", "Now Duplicate", "Sparta"))
    clock[0] += metadata.CACHE_SECONDS
    assert resolver.title_for(str(exe)).status == "ambiguous_manifest"


@pytest.mark.parametrize("limit", ["MAX_MANIFESTS", "MAX_ENTRIES", "MAX_TOTAL_BYTES"])
def test_incomplete_inventory_never_returns_partial_confidence(install, monkeypatch, limit):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest())
    monkeypatch.setattr(metadata, limit, 0)
    assert resolver.title_for(str(exe)).status == "metadata_scan_limit"


def test_cache_size_bound_and_eviction(install, tmp_path, monkeypatch):
    library, exe, resolver, clock = install
    monkeypatch.setattr(metadata, "MAX_CACHED_LIBRARIES", 2)
    for index in range(3):
        alternate = write(tmp_path / str(index) / "steamapps" / "common" / "Game" / "game.exe")
        resolver.title_for(str(alternate))
    assert len(resolver._cache) == 2
    assert all("/0/" not in str(root) for root in resolver._cache)


@pytest.mark.parametrize("path", [r"\\server\steamapps\common\Sparta\game.exe", "//server/steamapps/common/Sparta/game.exe", "relative/steamapps/common/Sparta/game.exe", "/fixture/steamapps/common/Sparta/../Other/game.exe", "/fixture/steamapps/common/Sparta/steamapps/common/Other/game.exe"])
def test_unsupported_or_ambiguous_paths_do_not_scan(path, monkeypatch):
    monkeypatch.setattr(metadata.os, "scandir", lambda *args: pytest.fail("Unsafe path scanned"))
    assert metadata.SteamTitleResolver().title_for(path).status == "unsupported_install_path"


def test_symlink_install_or_manifest_is_not_title_evidence(install, tmp_path):
    library, exe, resolver, clock = install
    target = write(tmp_path / "outside.acf", manifest())
    (library / "appmanifest_123.acf").symlink_to(target)
    assert resolver.title_for(str(exe)).status == "invalid_manifest"
    outside_exe = write(tmp_path / "OutsideGame" / "game.exe")
    link = library / "common" / "Linked"
    link.symlink_to(outside_exe.parent, target_is_directory=True)
    assert resolver.title_for(str(link / "game.exe")).status == "unverified_install_root"


def test_utility_filters_apply_before_metadata_and_to_product_title(install, monkeypatch):
    library, exe, resolver, clock = install
    with patch.object(metadata, "steam_title_for", side_effect=AssertionError("Utility read metadata")):
        assert GameManager._store_adapter(GameProcess(1, "wallpaper64.exe", str(library / "common/wallpaper_engine/wallpaper64.exe"), 1)) is None
        assert GameManager._store_adapter(GameProcess(1, "steam.exe", str(exe), 1)) is None
    write(library / "appmanifest_123.acf", manifest(name="Wallpaper Engine"))
    assert GameManager._store_adapter(GameProcess(1, "game.exe", str(exe), 1)) is None


def test_nonsteam_folder_is_explicit_unverified_and_does_not_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "steam_title_for", lambda *args: pytest.fail("Non-Steam lookup"))
    exe = str(tmp_path / "Epic Games" / "Codename" / "game.exe")
    adapter = GameManager._store_adapter(GameProcess(1, "game.exe", exe, 1))
    assert adapter.game_id == "store_codename"
    assert adapter.display_name == "Game folder: Codename"
    assert adapter.game_name_status == "unsupported_store_metadata"


def test_known_adapter_bypasses_metadata_and_preserves_title(install, monkeypatch):
    library, exe, resolver, clock = install
    manager = GameManager()
    manager._processes = lambda: iter([GameProcess(9, "eldenring.exe", str(exe), 2)])
    monkeypatch.setattr(metadata, "steam_title_for", lambda *args: pytest.fail("Known adapter lookup"))
    snap = manager.snapshot()
    assert (snap.game_id, snap.game_name) == ("elden_ring", "Elden Ring")
    assert snap.game_name_source == "builtin_process_signature"


def test_explicit_adapter_display_name_takes_precedence_without_metadata_read(install, monkeypatch):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest())
    manager = GameManager()
    chosen = GameAdapter("store_sparta", "My Chosen Label", ("game.exe",), ("my alias",),
                         game_name_source="user", game_name_status="user_supplied")
    manager.adapters.insert(0, chosen)
    manager._processes = lambda: iter([GameProcess(9, "game.exe", str(exe), 2)])
    monkeypatch.setattr(metadata, "steam_title_for", lambda *args: pytest.fail("User adapter was overridden"))
    snap = manager.snapshot()
    assert snap.game_id == "store_sparta" and snap.game_name == "My Chosen Label"
    assert snap.game_name_source == "user" and manager.adapters[0].aliases == ("my alias",)


def test_untrusted_fallback_directory_cannot_inject_multiline_title(tmp_path):
    exe = str(tmp_path / "Epic Games" / "Codename\nInjected\u202e" / "game.exe")
    adapter = GameManager._store_adapter(GameProcess(1, "game.exe", exe, 1))
    assert adapter.display_name == "Game folder: Codename Injected"


@pytest.mark.parametrize("code,expected", [
    ("ru", "Папка игры: Sparta"), ("en", "Game folder: Sparta"), ("de", "Spielordner: Sparta"),
    ("fr", "Dossier du jeu : Sparta"), ("es", "Carpeta del juego: Sparta"),
    ("it", "Cartella del gioco: Sparta"), ("pt", "Pasta do jogo: Sparta"),
])
def test_fallback_uses_existing_language_setting_and_preserves_folder(install, code, expected):
    library, exe, resolver, clock = install
    config = {"app.language": code}
    manager = GameManager(config=config)
    manager._processes = lambda: iter([GameProcess(1, "game.exe", str(exe), 1)])
    assert manager.snapshot().game_name == expected
    config["app.language"] = "en"
    assert manager.snapshot().game_name == "Game folder: Sparta"


def test_generic_foreground_preferred_and_pid_start_protections(install):
    library, exe, resolver, clock = install
    write(library / "appmanifest_123.acf", manifest())
    manager = GameManager()
    current = [GameProcess(19, "game.exe", str(exe), 100)]
    manager._processes = lambda: iter(current)
    service = GamePresenceService(manager)
    assert service.snapshot(force=True) is None  # a title cannot elevate a background store process
    with patch("jarvis.gaming.manager._foreground_pid", return_value=19):
        active = service.snapshot(force=True)
    assert active.foreground_verified and active.game_id == "store_sparta"
    assert service.snapshot(force=True).pid == 19  # previously verified session
    current[0] = GameProcess(19, "game.exe", str(exe), 200)  # PID reused by a new session
    assert service.snapshot(force=True) is None
    preferred = service.snapshot(force=True, preferred_pid=19, preferred_game_id="store_sparta")
    assert preferred.session_started_at == 200
    assert service.snapshot(force=True, preferred_pid=88, preferred_game_id="different") is not None  # prior live identity remains
    with patch("jarvis.gaming.manager._foreground_pid", return_value=999):
        assert service.snapshot(force=True, preferred_pid=19) is None
    assert manager.detect(preferred_pid=88, require_preferred=True) is None


def test_ambiguous_known_candidates_and_process_markers_stay_unchanged():
    manager = GameManager()
    manager._processes = lambda: iter([GameProcess(1, "eldenring.exe", "", 1), GameProcess(2, "Warframe.exe", "", 2)])
    assert GamePresenceService(manager).snapshot(force=True) is None
    adapter = GameAdapter("fixture", "Fixture", ("java.exe",), process_markers=("fixture-main",), marker_required_processes=("java.exe",))
    assert not adapter.matches_process(GameProcess(3, "java.exe", "", 1))
    assert adapter.matches_process(GameProcess(3, "java.exe", "", 1, ("fixture-main",)))


def test_all_detection_callers_pass_config_in_installed_source():
    source_root = Path(__file__).resolve().parents[1] / "jarvis"
    callers = (
        "agent.py", "game_web/service.py", "doctor.py", "gaming/memory.py",
        "gaming/speech_hints.py", "integrations/telegram_remote.py",
        "tools/implementations.py",
    )
    calls = 0
    for relative in callers:
        path = source_root / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "GameManager":
                calls += 1
                assert any(keyword.arg == "config" for keyword in node.keywords), relative
    assert calls == 9
