from pathlib import Path
import ast

from bootstrap import check_release_files
from jarvis.testing.build_integrity156 import write_manifest


def test_mixed_release_rejected_before_startup(tmp_path, capsys):
    internal = tmp_path/'_internal'
    internal.mkdir()
    (internal/'VERSION.txt').write_text('test-build')
    source = internal/'example.py'
    source.write_text('version = 1')
    write_manifest(tmp_path)
    assert check_release_files(tmp_path)
    source.write_text('version = 2')
    assert not check_release_files(tmp_path)
    assert 'BUILD_INTEGRITY=' in capsys.readouterr().out


def test_source_checkout_remains_available(tmp_path):
    assert check_release_files(tmp_path)


def test_app_checks_integrity_before_instance_or_audio_setup():
    source = Path(__file__).resolve().parents[1]/'app.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    calls = sorted((n.lineno, ast.unparse(n.func)) for n in ast.walk(main) if isinstance(n, ast.Call))
    check = next(line for line, name in calls if name == 'check_release_files')
    assert check < next(line for line, name in calls if name == 'InstanceCoordinator')
    assert check < next(line for line, name in calls if name == 'ConfigManager')
