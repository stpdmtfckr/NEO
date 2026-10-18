import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('text_stats_plugin', Path(__file__).resolve().parents[1] / 'plugin.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def test_counts():
    assert plugin.stats('Привет, мир. Как дела?') == {'words': 4, 'chars': 22, 'sentences': 2}


def test_empty():
    assert plugin.stats('') == {'words': 0, 'chars': 0, 'sentences': 0}
