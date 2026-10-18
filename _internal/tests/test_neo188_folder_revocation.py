"""NEO 188 NEO-PRIVACY-001 / TZ-KNOWLEDGE-FOLDER-REVOCATION-IDENTITY-01 (synthetic grants only)."""
from types import SimpleNamespace

import pytest

from jarvis.runtime import knowledge169
from jarvis.runtime.conversation import ConversationState
from jarvis.runtime.dialogue_tasks import prepare_dialogue, record_attempt
from jarvis.runtime.knowledge_phrases188 import parse
from jarvis.tools import knowledge_tool169
from jarvis.tools.registry import ToolExecution

A, B, C = r'C:\Users\U\Work\Docs', r'D:\Archive\Docs', r'C:\Users\U\Music'


class Index:
    def __init__(self, fail=False):
        self.dropped, self.fail = [], fail

    def drop_folder(self, folder):
        if self.fail:
            raise OSError('index locked')
        self.dropped.append(folder)
        return 3


def service(folders, *, index=None, save_fail=False):
    data = {'knowledge': {'folders': list(folders)}}
    config = SimpleNamespace(data=data, get=lambda key, default=None: data['knowledge']['folders'] if key == 'knowledge.folders' else default)
    svc = knowledge169.KnowledgeService.__new__(knowledge169.KnowledgeService)
    svc.config = config
    svc.index = index or Index()

    def save(new):
        if save_fail:
            raise OSError('disk full')
        data['knowledge']['folders'] = list(new)
    svc._save_folders = save
    return svc, data


def run(svc, monkeypatch, folder, code='ru'):
    monkeypatch.setattr(knowledge169, 'service', lambda config: svc)
    monkeypatch.setattr(knowledge_tool169, '_code', lambda config: code)
    return knowledge_tool169.run(svc.config, None, {'action': 'remove_folder', 'folder': folder})


def test_kfri01_ambiguous_short_name_changes_nothing_and_lists_full_paths(monkeypatch):
    svc, data = service([A, B, C])
    result = run(svc, monkeypatch, 'Docs')
    assert not result.success and result.data['outcome'] == 'ambiguous'
    assert A in result.text and B in result.text and data['knowledge']['folders'] == [A, B, C]
    assert svc.index.dropped == []


def test_kfri02_choice_is_bound_to_the_exact_path_and_revokes_only_it(monkeypatch):
    svc, data = service([A, B, C])
    state = ConversationState()
    first = run(svc, monkeypatch, 'Docs')
    record_attempt(state, 'knowledge', {'action': 'remove_folder', 'folder': 'Docs'}, first, now=10)
    chosen = prepare_dialogue('2', 'ru', state, lambda v: None, now=11)
    assert chosen.feature == ('knowledge', {'action': 'remove_folder', 'folder': B})
    done = run(svc, monkeypatch, B)
    assert done.success and done.data == {'outcome': 'removed', 'folder': B} and B in done.text
    assert data['knowledge']['folders'] == [A, C] and svc.index.dropped == [B]


def test_kfri03_unique_short_name_resolves_to_its_full_path(monkeypatch):
    svc, data = service([A, C])
    result = run(svc, monkeypatch, 'Music')
    assert result.success and result.data['folder'] == C and C in result.text


def test_kfri04_repeated_exact_operation_is_idempotent(monkeypatch):
    svc, data = service([A, B])
    assert run(svc, monkeypatch, B).success
    again = run(svc, monkeypatch, B)
    assert not again.success and again.data['outcome'] == 'missing'
    assert data['knowledge']['folders'] == [A]          # never moves on to the other «Docs»


def test_kfri05_save_failure_and_index_failure_are_distinct(monkeypatch):
    svc, data = service([A, C], save_fail=True)
    failed = run(svc, monkeypatch, C)
    assert not failed.success and failed.data['outcome'] == 'save_failed' and 'прежним' in failed.text
    svc, data = service([A, C], index=Index(fail=True))
    partial = run(svc, monkeypatch, C)
    assert partial.success and partial.data['outcome'] == 'index_failed' and 'индексе' in partial.text
    assert data['knowledge']['folders'] == [A]


def test_kfri06_path_not_granted_is_not_matched_by_its_last_part(monkeypatch):
    svc, data = service([A])
    result = run(svc, monkeypatch, r'E:\Other\Docs')
    assert not result.success and result.data['outcome'] == 'missing' and data['knowledge']['folders'] == [A]


def test_kfri07_no_confirmation_is_needed_when_nothing_would_change():
    from jarvis.security import Risk
    from jarvis.tools import integrations
    svc, _ = service([A, B])
    original = knowledge169.service
    knowledge169.service = lambda config: svc
    try:
        assert integrations._knowledge_risk188(svc.config, {'action': 'remove_folder', 'folder': 'Docs'}) == Risk.SAFE
        assert integrations._knowledge_risk188(svc.config, {'action': 'remove_folder', 'folder': A}) == Risk.MEDIUM
    finally:
        knowledge169.service = original


@pytest.mark.parametrize('code,phrase', [
    ('ru', 'больше не ищи в папке Документы'), ('ru', 'запрети искать в папке Документы'),
    ('en', 'stop searching the folder Documents'), ('de', 'suche nicht mehr im Ordner Dokumente'),
    ('fr', 'ne cherche plus dans le dossier Documents'), ('es', 'no busques más en la carpeta Documentos'),
    ('it', 'non cercare più nella cartella Documenti'), ('pt', 'não procures mais na pasta Documentos'),
])
def test_kfri08_negated_search_is_a_revocation_never_its_opposite(code, phrase):
    tool, args = parse(phrase, code)
    assert tool == 'knowledge' and args['action'] == 'remove_folder' and args['folder']
