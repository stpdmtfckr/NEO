"""Real registered filesystem handler, real temporary files, no desktop actions."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.security import SecurityPolicy
from jarvis.tools import implementations


@pytest.fixture
def filesystem(tmp_path, monkeypatch):
    files = tmp_path / "Мои файлы 日本語"
    files.mkdir()
    values = {"security.full_user_file_access": False,
              "security.restrict_file_mutations": True,
              "security.allowed_roots": [str(files)]}
    config = SimpleNamespace(
        paths=SimpleNamespace(root=tmp_path, backups=tmp_path / "backups", cache=tmp_path / "cache"),
        get=lambda key, default=None: values.get(key, default),
    )
    # Registration is real; stop after the filesystem definition so unrelated
    # plugin services are not installed or started by this focused test.
    class Captured(Exception):
        pass

    class Registry:
        def __init__(self):
            self.config, self.security = config, SecurityPolicy(config)
            self.database, self.ollama = None, None
            self.definition = None

        def register(self, definition):
            assert definition.name == "filesystem"
            self.definition = definition
            raise Captured

    import jarvis.integrations.discord_navigation as discord
    monkeypatch.setattr(discord, "_SHARED_ADAPTERS", {})
    monkeypatch.setattr(implementations, "_sync_open_text_window", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(implementations, "_edit_active_text_window", lambda *_args, **_kwargs: False)
    registry = Registry()
    with pytest.raises(Captured):
        implementations.register_all(registry)
    definition = registry.definition

    def run(action, **arguments):
        result = definition.handler({"action": action, **arguments})
        assert result.success, result.text
        return result

    yield files, run
    registry.spotify_adapter.session.close()


def test_unicode_create_write_append_replace_read_and_undo(filesystem, monkeypatch):
    # All edits share one wall-clock second, deterministically exercising the
    # collision that previously lost earlier snapshots. File I/O remains real.
    from datetime import datetime
    class SameSecond(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 18, 12, 0, 0, tzinfo=tz)
    monkeypatch.setattr(implementations, "datetime", SameSecond)
    files, run = filesystem
    folder = files / "Проект ação"
    run("create_folder", destination=str(folder))
    assert folder.is_dir()
    path = folder / "заметка 日本語.txt"
    states = ["Привет — Olá 🌍\n", "Первый текст\n", "Первый текст\nДополнение ñ\n", "Новый текст\nДополнение ñ\n"]
    run("create_file", folder=str(folder), name=path.name, content=states[0])
    assert path.read_bytes() == states[0].encode("utf-8")
    run("write_text", source=str(path), content=states[1])
    run("append_text", source=str(path), content="Дополнение ñ\n")
    run("replace_text", source=str(path), old_text="Первый", content="Новый")
    assert path.read_bytes() == states[-1].encode("utf-8")
    assert run("read_text", source=str(path)).data == states[-1]
    for expected in reversed(states[:-1]):
        run("undo_last_change")
        assert path.read_bytes() == expected.encode("utf-8")


def test_binary_copy_move_rename_preserve_all_bytes(filesystem):
    files, run = filesystem
    original = files / "оригинал.bin"
    payload = bytes(range(256)) * 37 + b"\x00\xff\r\n"
    original.write_bytes(payload)
    copied = files / "копия.bin"
    run("copy", source=str(original), destination=str(copied))
    assert copied.read_bytes() == payload
    assert original.read_bytes() == payload
    moved = files / "перемещено.bin"
    run("move", source=str(copied), destination=str(moved))
    assert not copied.exists()
    assert moved.read_bytes() == payload
    run("rename", source=str(moved), new_name="новое имя 日本語")
    renamed = files / "новое имя 日本語.bin"
    assert not moved.exists()
    assert renamed.read_bytes() == payload
    assert original.read_bytes() == payload


def test_zip_roundtrip_preserves_nested_unicode_and_binary(filesystem):
    files, run = filesystem
    source = files / "исходная папка"
    (source / "nested").mkdir(parents=True)
    (source / "привет.txt").write_text("Hallo, мир!", encoding="utf-8")
    (source / "nested/data.bin").write_bytes(bytes(range(256)))
    archive, target = files / "bundle.zip", files / "распаковано"
    run("zip", source=str(source), destination=str(archive))
    run("unzip", source=str(archive), destination=str(target))
    for path in source.rglob("*"):
        if path.is_file():
            assert (target / source.name / path.relative_to(source)).read_bytes() == path.read_bytes()


def test_failed_edit_and_unbound_filename_leave_existing_file_unchanged(filesystem):
    files, run = filesystem
    path = files / "сохранить.txt"
    before = "Содержимое не должно потеряться".encode("utf-8")
    path.write_bytes(before)
    with pytest.raises(ValueError):
        run("replace_text", source=str(path), old_text="несуществующий фрагмент", content="new")
    with pytest.raises(ValueError):
        run("write_text", source=path.name, content="wrong target")
    assert path.read_bytes() == before


def test_write_outside_allowed_test_root_is_rejected(filesystem, tmp_path):
    files, run = filesystem
    outside = tmp_path / "other" / "private.txt"
    outside.parent.mkdir()
    outside.write_text("Keep this", encoding="utf-8")
    with pytest.raises(PermissionError):
        run("write_text", source=str(outside), content="Overwrite")
    assert outside.read_text(encoding="utf-8") == "Keep this"


def test_recycle_delete_and_undo_restore_exact_original_bytes(filesystem, monkeypatch):
    files, run = filesystem
    path = files / "важный файл.bin"
    payload = bytes(range(256)) + b"\x00\xffOriginal"
    path.write_bytes(payload)
    trash = files / "test trash"
    trash.mkdir()

    def local_trash(value):
        source = Path(value)
        assert source.parent == files
        source.rename(trash / source.name)

    monkeypatch.setattr(implementations, "send2trash", local_trash)
    run("delete", source=str(path))
    assert not path.exists()
    assert (trash / path.name).read_bytes() == payload
    run("undo_last_change")
    assert path.read_bytes() == payload
