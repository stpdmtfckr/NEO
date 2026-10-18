"""Pure startup diagnostics: no Qt, audio, network, profile or application import."""
import ast
import hashlib
import json
import logging
import os
from pathlib import Path
import sys

import pytest

from jarvis.runtime import build_identity as identity


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "_internal"
    for name in identity.SUBSET_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("fixture:" + name + "\n").encode())
    (root / "VERSION.txt").write_text("NEO v4.0.20.184-TEST\n")
    return root


def payload_files(source):
    files = {"_internal/" + name: hashlib.sha256((source / name).read_bytes()).hexdigest()
             for name in identity.SUBSET_FILES}
    files["_internal/VERSION.txt"] = hashlib.sha256((source / "VERSION.txt").read_bytes()).hexdigest()
    return files


def stamp(source, revision="checkpoint20-test"):
    content = identity.metadata_bytes(revision, payload_files(source))
    (source / identity.METADATA_NAME).write_bytes(content)
    return json.loads(content)


def test_valid_identity_distinguishes_declaration_from_fresh_partial_verification(source):
    metadata = stamp(source)
    result = identity.read_startup_identity(source)
    assert result["packaged_status"] == "declared"
    assert result["packaged_revision"] == "checkpoint20-test"
    assert result["packaged_payload_sha256"] == metadata["payload_sha256"]
    assert result["payload_verification"] == "not_checked_at_startup"
    assert result["subset_status"] == "match"
    assert result["local_subset_sha256"] == identity.manifest_fingerprint(metadata["subset_files"])
    assert result["subset_files"] == len(identity.SUBSET_FILES)
    assert result["subset_bytes"] == sum((source / name).stat().st_size for name in identity.SUBSET_FILES)


def test_changed_checked_entrypoint_is_visible_without_changing_declared_revision(source):
    stamp(source)
    before = identity.read_startup_identity(source)
    (source / "jarvis/wake_matcher.py").write_bytes(b"local change\n")
    after = identity.read_startup_identity(source)
    assert after["packaged_revision"] == before["packaged_revision"]
    assert after["packaged_payload_sha256"] == before["packaged_payload_sha256"]
    assert after["subset_status"] == "mismatch"
    assert after["local_subset_sha256"] != before["local_subset_sha256"]


def test_changes_outside_subset_remain_explicitly_unverified(source):
    stamp(source)
    before = identity.read_startup_identity(source)
    (source / "VERSION.txt").write_text("changed release\n")
    assert identity.read_startup_identity(source) == before
    assert before["payload_verification"] == "not_checked_at_startup"


def test_source_checkout_without_metadata_still_has_fresh_bounded_identity(source):
    result = identity.read_startup_identity(source)
    assert result["packaged_status"] == "missing"
    assert result["packaged_revision"] == "unavailable"
    assert result["subset_status"] == "uncompared"
    assert len(result["local_subset_sha256"]) == 64


@pytest.mark.parametrize("corruption", [
    "invalid_json", "invalid_utf8", "array", "deep_json", "oversized", "duplicate_keys",
    "schema", "boolean_schema", "revision_newline", "revision_long", "revision_unicode",
    "payload_digest", "payload_scope", "subset_scope", "extra_field", "arbitrary_path", "subset_digest",
])
def test_invalid_metadata_has_fixed_status_and_cannot_echo_content(source, corruption):
    value = stamp(source)
    secret = "DO_NOT_ECHO_THIS_SENTINEL"
    content = None
    if corruption == "invalid_json": content = (secret + "{").encode()
    elif corruption == "invalid_utf8": content = b"\xff" + secret.encode()
    elif corruption == "array": content = json.dumps([secret]).encode()
    elif corruption == "deep_json": content = ("[" * 1500 + "0" + "]" * 1500).encode()
    elif corruption == "oversized": content = (secret * identity.MAX_METADATA_BYTES).encode()
    elif corruption == "duplicate_keys": content = ('{"schema":1,"schema":2,"x":"' + secret + '"}').encode()
    elif corruption == "schema": value["schema"] = 2
    elif corruption == "boolean_schema": value["schema"] = True
    elif corruption == "revision_newline": value["revision"] = secret + "\ninjected=true"
    elif corruption == "revision_long": value["revision"] = secret * 4
    elif corruption == "revision_unicode": value["revision"] = secret + "☃"
    elif corruption == "payload_digest": value["payload_sha256"] = secret
    elif corruption == "payload_scope": value["payload_scope"] = secret
    elif corruption == "subset_scope": value["subset_scope"] = secret
    elif corruption == "extra_field": value[secret] = secret
    elif corruption == "arbitrary_path": value["subset_files"]["../" + secret] = "a" * 64
    elif corruption == "subset_digest": value["subset_files"][identity.SUBSET_FILES[0]] = secret
    (source / identity.METADATA_NAME).write_bytes(content if content is not None else json.dumps(value).encode())
    result = identity.read_startup_identity(source)
    assert result["packaged_status"] == "invalid"
    assert result["packaged_revision"] == "unavailable"
    assert result["packaged_payload_sha256"] == "unavailable"
    assert result["subset_status"] == "uncompared"
    line = identity.startup_identity_line(source)
    assert secret not in line and "\n" not in line and "injected" not in line


@pytest.mark.parametrize("failure", ["missing", "directory", "oversized", "symlink", "linked_parent", "total_limit"])
def test_unreadable_or_unbounded_sources_never_claim_verification(source, failure, monkeypatch):
    stamp(source)
    target = source / identity.SUBSET_FILES[0]
    if failure == "missing": target.unlink()
    elif failure == "directory":
        target.unlink()
        target.mkdir()
    elif failure == "oversized": target.write_bytes(b"x" * (identity.MAX_SOURCE_FILE_BYTES + 1))
    elif failure == "symlink":
        target.unlink()
        target.symlink_to(source / "VERSION.txt")
    elif failure == "linked_parent":
        path = source / "jarvis/runtime"
        moved = source / "actual_runtime"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    elif failure == "total_limit": monkeypatch.setattr(identity, "MAX_SUBSET_BYTES", 1)
    result = identity.read_startup_identity(source)
    assert result["packaged_status"] == "declared"
    assert result["subset_status"] == "unavailable"
    assert result["local_subset_sha256"] == "unavailable"


def test_startup_reads_only_fixed_regular_files_and_never_walks_tree(source, monkeypatch):
    stamp(source)
    opened = []
    original = identity._open_beneath
    def record(root, name):
        assert root == source
        opened.append(name)
        return original(root, name)
    monkeypatch.setattr(identity, "_open_beneath", record)
    monkeypatch.setattr(Path, "rglob", lambda *a, **kw: pytest.fail("startup walked the tree"))
    assert identity.read_startup_identity(source)["subset_status"] == "match"
    assert opened == [identity.METADATA_NAME, *identity.SUBSET_FILES]


def test_fingerprint_is_order_independent_and_includes_relative_paths():
    files = {"a.py": "a" * 64, "b.py": "b" * 64}
    assert identity.manifest_fingerprint(files) == identity.manifest_fingerprint(dict(reversed(list(files.items()))))
    assert identity.manifest_fingerprint(files) != identity.manifest_fingerprint({"c.py": "a" * 64, "b.py": "b" * 64})


def test_packager_cannot_fingerprint_identity_metadata_itself(source):
    payload = payload_files(source)
    payload["_internal/" + identity.METADATA_NAME] = "0" * 64
    with pytest.raises(ValueError, match="must_be_excluded"):
        identity.metadata_bytes("checkpoint20", payload)


def test_unexpected_failure_is_fixed_and_nonfatal(source, monkeypatch):
    def fail(*args):
        raise RuntimeError("private exception contents")
    monkeypatch.setattr(identity, "read_startup_identity", fail)
    assert identity.startup_identity_line(source) == (
        "NEO source identity: packaged_status=unavailable subset_status=unavailable reason=diagnostic_error")


@pytest.mark.parametrize("version_exists", [True, False])
def test_actual_app_startup_fragment_preserves_release_output_and_adds_one_identity_line(
        source, capsys, caplog, version_exists):
    # Execute the actual startup logging fragment without importing app.py/Qt.
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    start = next(i for i, node in enumerate(main.body) if isinstance(node, ast.Try)
                 and any(isinstance(n, ast.Name) and n.id == "release_version" for n in ast.walk(node)))
    end = next(i for i, node in enumerate(main.body) if i > start and isinstance(node, ast.Expr)
               and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
               and node.value.func.attr == "info"
               and any(isinstance(n, ast.Name) and n.id == "source_marker" for n in ast.walk(node)))
    fragment = ast.Module(body=main.body[start:end + 1], type_ignores=[])
    if not version_exists:
        (source / "VERSION.txt").unlink()
    release = "NEO v4.0.20.184-TEST" if version_exists else "NEO v4.0.20 ALPHA-BETA"
    namespace = {"root": source, "sys": sys, "logging": logging, "__name__": "startup-test"}
    with caplog.at_level(logging.INFO):
        exec(compile(fragment, str(app_path), "exec"), namespace)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "NEO build: " + release
    assert lines[1] == identity.startup_identity_line(source)
    assert len(lines) == 2
    assert [record.message for record in caplog.records] == lines


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative race injection")
@pytest.mark.parametrize("replacement", ["file_link", "directory_link", "fifo"])
def test_descriptor_open_race_cannot_redirect_content_reads(source, tmp_path, monkeypatch, replacement):
    stamp(source)
    original = os.open
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.py").write_bytes(b"must never be read")
    injected = []
    def swap(path, flags, *args, **kwargs):
        target = "runtime" if replacement == "directory_link" else "app.py"
        if path == target and not injected:
            injected.append(True)
            if replacement == "directory_link":
                victim = source / "jarvis/runtime"
                victim.rename(source / "saved_runtime")
                victim.symlink_to(outside, target_is_directory=True)
            else:
                victim = source / "app.py"
                victim.unlink()
                if replacement == "file_link": victim.symlink_to(outside / "outside.py")
                else: os.mkfifo(victim)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swap)
    assert identity.read_startup_identity(source)["subset_status"] == "unavailable"
    assert injected


def test_unsupported_safe_open_is_explicit_and_nonfatal(source, monkeypatch):
    def unsupported(*args): raise identity.SafeOpenUnavailable("unsupported private detail")
    monkeypatch.setattr(identity, "_open_beneath", unsupported)
    result = identity.read_startup_identity(source)
    assert result["packaged_status"] == result["subset_status"] == "unavailable"
    assert "private" not in identity.startup_identity_line(source)


class FakeWindowsApi:
    def __init__(self, final_path, *, regular=True, fail=None):
        self.path, self.is_regular, self.fail = final_path, regular, fail
        self.closed, self.transfers = [], []
    def open(self, path):
        if self.fail == "open": raise OSError("fake private detail")
        self.opened = path
        return 101
    def regular(self, handle):
        if self.fail == "regular": raise OSError("fake private detail")
        return self.is_regular
    def final_path(self, handle):
        if self.fail == "final_path": raise OSError("fake private detail")
        return self.path
    def to_fd(self, handle):
        if self.fail == "transfer": raise OSError("fake private detail")
        self.transfers.append(handle)
        return 102
    def close(self, handle): self.closed.append(handle)


@pytest.mark.parametrize("actual", [
    r"C:\outside\app.py", r"C:\NEO-other\app.py", r"C:\NEO\other.py",
    r"C:\neo\app.py", r"\\?\C:\outside\app.py",
])
def test_windows_adapter_rejects_redirected_or_nonexact_path_before_read(actual):
    api = FakeWindowsApi(actual)
    with pytest.raises(ValueError, match="unavailable"):
        identity._open_windows_file(r"C:\NEO\app.py", api)
    assert api.closed == [101] and not api.transfers


@pytest.mark.parametrize("failure", ["open", "regular", "final_path", "transfer", "nonregular"])
def test_windows_adapter_closes_every_acquired_rejected_handle(failure):
    api = FakeWindowsApi(r"C:\NEO\app.py", regular=failure != "nonregular", fail=failure)
    with pytest.raises((ValueError, OSError)):
        identity._open_windows_file(r"C:\NEO\app.py", api)
    assert api.closed == ([] if failure == "open" else [101])
    assert not api.transfers


@pytest.mark.parametrize(("expected", "actual"), [
    (r"C:\NEO\app.py", r"\\?\C:\NEO\app.py"),
    (r"\\server\share\NEO\app.py", r"\\?\UNC\server\share\NEO\app.py"),
])
def test_windows_adapter_transfers_only_validated_exact_handle(expected, actual):
    api = FakeWindowsApi(actual)
    assert identity._open_windows_file(expected, api) == 102
    assert api.transfers == [101] and not api.closed


@pytest.mark.parametrize("fault", [None, "reparse", "directory", "nondisk", "query", "long_path", "empty_path", "missing", "denied"])
def test_win32_bridge_flags_fixed_buffers_and_ownership_with_fake_kernel(monkeypatch, fault):
    import ctypes
    from types import SimpleNamespace
    events = []
    class Function:
        def __init__(self, implementation): self.implementation = implementation
        def __call__(self, *args): return self.implementation(*args)
    def create(path, access, sharing, security, disposition, flags, template):
        events.append(("create", access, sharing, disposition, flags, security, template))
        return ctypes.c_void_p(-1).value if fault in {"missing", "denied"} else 101
    def information(handle, kind, buffer, size):
        assert kind == 9 and size == 8
        buffer[0] = 0x400 if fault == "reparse" else 0x10 if fault == "directory" else 0
        return fault != "query"
    def final_path(handle, buffer, size, flags):
        assert size == 32768 and flags == 0
        if fault == "long_path": return size + 1
        if fault == "empty_path": return 0
        buffer.value = r"\\?\C:\NEO\app.py"
        return len(buffer.value)
    kernel = SimpleNamespace(
        CreateFileW=Function(create),
        CloseHandle=Function(lambda handle: events.append(("close", handle)) or 1),
        GetFileType=Function(lambda handle: 3 if fault == "nondisk" else 1),
        GetFileInformationByHandleEx=Function(information),
        GetFinalPathNameByHandleW=Function(final_path),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 2 if fault == "missing" else 5, raising=False)
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        open_osfhandle=lambda handle, flags: events.append(("transfer", handle, flags)) or 102))
    if fault is None:
        assert identity._open_windows_file(r"C:\NEO\app.py") == 102
        assert ("transfer", 101, 0x8000) in events and ("close", 101) not in events
    else:
        with pytest.raises((ValueError, OSError)):
            identity._open_windows_file(r"C:\NEO\app.py")
        assert not any(event[0] == "transfer" for event in events)
        assert events.count(("close", 101)) == (0 if fault in {"missing", "denied"} else 1)
    assert events[0] == ("create", 0x80000000, 7, 3, 0x02200000, None, None)


def test_windows_missing_api_is_explicitly_unavailable(monkeypatch):
    def missing(): raise AttributeError("private platform detail")
    monkeypatch.setattr(identity, "_WindowsFileApi", missing)
    with pytest.raises(identity.SafeOpenUnavailable, match="safe_open_unavailable"):
        identity._open_windows_file(r"C:\NEO\app.py")


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows file handles")
def test_native_windows_safe_reader_and_descriptor_cleanup(source):
    stamp(source)
    assert identity.read_startup_identity(source)["subset_status"] == "match"
    # Successful read must not leave an open descriptor blocking file removal.
    (source / "app.py").unlink()
    assert identity.read_startup_identity(source)["subset_status"] == "unavailable"


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows symlink support")
def test_native_windows_redirected_parent_is_not_read(source, tmp_path):
    stamp(source)
    victim = source / "jarvis/runtime"
    moved = tmp_path / "outside_runtime"
    victim.rename(moved)
    try:
        victim.symlink_to(moved, target_is_directory=True)
    except OSError:
        moved.rename(victim)
        pytest.skip("Windows symlink privilege unavailable")
    assert identity.read_startup_identity(source)["subset_status"] == "unavailable"
