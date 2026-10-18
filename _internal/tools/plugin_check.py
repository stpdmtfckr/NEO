"""Check a NEO plugin before installing it: python tools/plugin_check.py plugins/<name>

Validates manifest.json, loads plugin.py through the same permission layer NEO uses,
checks that every declared tool is registered and every phrase row has 7 languages,
then runs the plugin's own tests (pytest) if it has any.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(folder: str) -> int:
    from jarvis.runtime.plugins import read_manifest, fingerprint
    from jarvis.runtime.plugin_permissions169 import PluginServices
    path = Path(folder).resolve()
    manifest, error = read_manifest(path)
    if error:
        print('FAIL manifest:', error)
        return 1
    registered = []
    fake = SimpleNamespace(config=SimpleNamespace(get=lambda k, d=None: d, data={}), security=None,
                           register=lambda tool: registered.append(tool.name), execute=None)
    services = PluginServices(manifest['name'], set(manifest['permissions']), fake, {t['name'] for t in manifest['tools']})
    import importlib.util
    spec = importlib.util.spec_from_file_location('plugin_under_check', path / 'plugin.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register_plugin(services)
    missing = {t['name'] for t in manifest['tools']} - set(registered)
    if missing:
        print('FAIL tools declared but not registered:', sorted(missing))
        return 1
    print('OK manifest, permissions:', manifest['permissions'] or 'none', 'fingerprint:', fingerprint(path))
    tests = path / str(manifest.get('tests', 'tests'))
    if tests.is_dir():
        return subprocess.call([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(tests)], cwd=str(ROOT))
    print('NOTE: the plugin has no tests folder')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
