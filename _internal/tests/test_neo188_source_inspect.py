"""NEO 188 NEO-DOC-002 / TZ-ANSWER-SOURCE-EXCERPT-INSPECTION-01 (synthetic files, nothing is opened)."""
import os
from types import SimpleNamespace

import pytest

from jarvis.runtime import knowledge169, local_chat169
from jarvis.runtime import source_inspect188 as inspect
from jarvis.tools import knowledge_tool169


@pytest.fixture
def env(tmp_path, monkeypatch):
    folder = tmp_path / 'docs'
    folder.mkdir()
    long_text = 'Kazan trip notes. ' + 'x' * 2000 + ' tail-not-passed'
    (folder / 'trip.md').write_text('# Plan\n' + long_text + '\n', encoding='utf-8')
    index = knowledge169.KnowledgeIndex(tmp_path / 'idx.sqlite3')
    index.update_folders([str(folder)], pause=0)
    folders = [str(folder)]
    service = SimpleNamespace(index=index, folders=lambda: folders, busy=lambda: False,
                              search=lambda q, limit=5: index.search(q, limit))
    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None: default)
    monkeypatch.setattr(knowledge169, 'service', lambda cfg: service)
    monkeypatch.setattr(knowledge_tool169, '_code', lambda cfg: 'en')
    monkeypatch.setattr(local_chat169, 'answer', lambda *a, **k: 'The trip is to Kazan [1].')
    inspect.forget(str(tmp_path))
    return tmp_path, folder, folders, config


def run(config, args):
    return knowledge_tool169.run(config, None, args)


def test_asi01_exact_excerpt_passed_to_the_model_is_shown(env):
    tmp, folder, folders, config = env
    answer = run(config, {'action': 'ask', 'query': 'Kazan trip'})
    assert answer.success and 'show source 1' in answer.text
    shown = run(config, {'action': 'show_source', 'index': 1})
    assert shown.success and str(folder / 'trip.md') in shown.text
    assert 'Excerpt passed to the model' in shown.text and 'tail-not-passed' not in shown.text
    assert 'does not prove every statement' in shown.text


def test_asi02_bad_number_and_no_answer(env):
    tmp, folder, folders, config = env
    assert run(config, {'action': 'show_source', 'index': 1}).data['outcome'] == 'no_answer'
    run(config, {'action': 'ask', 'query': 'Kazan trip'})
    assert run(config, {'action': 'show_source', 'index': 9}).data['outcome'] == 'bad_index'


def test_asi04_revoked_folder_is_not_shown_from_history(env):
    tmp, folder, folders, config = env
    run(config, {'action': 'ask', 'query': 'Kazan trip'})
    folders.clear()
    result = run(config, {'action': 'show_source', 'index': 1})
    assert not result.success and result.data['outcome'] == 'revoked'
    assert not run(config, {'action': 'open_source', 'index': 1}).success


def test_asi06_changed_or_deleted_original_is_labelled(env):
    tmp, folder, folders, config = env
    run(config, {'action': 'ask', 'query': 'Kazan trip'})
    path = folder / 'trip.md'
    path.write_text('# Plan\nchanged\n', encoding='utf-8')
    os.utime(path, (1, 1))
    changed = run(config, {'action': 'show_source', 'index': 1})
    assert changed.data['state'] == 'changed' and 'shown as it was used' in changed.text and 'Kazan trip notes' in changed.text
    path.unlink()
    missing = run(config, {'action': 'show_source', 'index': 1})
    assert missing.data['state'] == 'missing'
    assert run(config, {'action': 'open_source', 'index': 1}).data['outcome'] == 'missing'


def test_asi05_executables_are_never_opened(tmp_path):
    row = {'n': 1, 'side': '', 'file': str(tmp_path / 'docs' / 'tool.exe'), 'location': '', 'excerpt': 'x', 'identity': None, 'passed_to_model': True}
    assert os.path.splitext(row['file'])[1] not in inspect.OPENABLE


def test_asi07_no_model_shows_retrieved_fragments_qualified(env, monkeypatch):
    tmp, folder, folders, config = env
    monkeypatch.setattr(local_chat169, 'answer', lambda *a, **k: None)
    run(config, {'action': 'ask', 'query': 'Kazan trip'})
    shown = run(config, {'action': 'show_source', 'index': 1})
    assert 'Retrieved fragment (no model was used)' in shown.text


@pytest.mark.parametrize('code,phrase,action', [
    ('ru', 'покажи источник 2', 'show_source'), ('ru', 'открой источник 1', 'open_source'), ('ru', 'открой источник 1 в папке', 'reveal_source'),
    ('en', 'show source 2', 'show_source'), ('de', 'zeige Quelle 2', 'show_source'), ('fr', 'montre la source 2', 'show_source'),
    ('es', 'muestra la fuente dos', 'show_source'), ('it', 'apri la fonte 1', 'open_source'), ('pt', 'mostra a fonte 2', 'show_source'),
])
def test_asi08_phrases(code, phrase, action):
    tool, args = inspect.parse(phrase, code)
    assert tool == 'knowledge' and args['action'] == action and args['index'] in {1, 2}
