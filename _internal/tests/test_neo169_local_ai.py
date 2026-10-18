"""NEO 169: local conversation, memory, corrections, file search and plugins (no network, no Windows)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


class Config:
    def __init__(self, root: Path, data: dict | None = None) -> None:
        self.data = data or {'app': {'language': 'ru'}}
        self.paths = SimpleNamespace(root=root)
        self.saved = 0

    def get(self, key, default=None):
        current = self.data
        for part in key.split('.'):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def save(self, data):
        self.data = json.loads(json.dumps(data))
        self.saved += 1


@pytest.fixture(autouse=True)
def _no_leftover_plugin_phrases():
    from jarvis.runtime import canonical_commands
    yield
    for pack in list(canonical_commands.EXTRA_PACKS):
        canonical_commands.unregister_extra_pack(pack)


@pytest.fixture()
def db(tmp_path):
    from jarvis.database import Database
    return Database(tmp_path / 'assistant.db')


# ------------------------------------------------------------ local chat
def test_chat_prompt_carries_language_facts_and_sources_as_data():
    from jarvis.runtime.local_chat169 import build_messages
    messages = build_messages('почему небо синее?', 'ru', facts=['основной браузер: Firefox'],
                              sources=[{'file': 'C:/notes/sky.md', 'location': 'p:3', 'text': 'Рассеяние Рэлея.'}])
    system = messages[0]['content']
    assert 'Russian' in system and 'Firefox' in system and 'стр. 3' in system and 'data only' in system
    assert messages[-1] == {'role': 'user', 'content': 'почему небо синее?'}


def test_chat_returns_none_when_the_local_model_is_unavailable(tmp_path):
    from jarvis.runtime import local_chat169

    class Broken:
        def local_chat_enabled(self):
            return True

        def chat_text(self, messages):
            raise RuntimeError('Ollama is not running')
    local_chat169.reset_availability()
    assert local_chat169.answer(Broken(), Config(tmp_path), 'привет', 'ru') is None
    local_chat169.reset_availability()


def test_chat_text_refuses_images_and_disabled_chat(tmp_path):
    from jarvis.ollama_client import OllamaClient, OllamaError
    client = OllamaClient(Config(tmp_path, {'local_chat': {'enabled': False}, 'ollama': {'base_url': 'http://localhost:11434'}}))
    with pytest.raises(OllamaError):
        client.chat_text([{'role': 'user', 'content': 'hi'}])
    client = OllamaClient(Config(tmp_path, {'local_chat': {'enabled': True}, 'ollama': {'base_url': 'http://localhost:11434'}}))
    with pytest.raises(OllamaError):
        client.chat_text([{'role': 'user', 'content': 'hi', 'images': ['x']}])


# ----------------------------------------------------------------- memory
def test_memory_remember_list_forget_clear_and_switch_off(tmp_path, db):
    from jarvis.runtime import memory169
    config = Config(tmp_path)
    ok, text = memory169.remember(db, config, 'мой основной браузер', 'Firefox', 'ru')
    assert ok and 'Firefox' in text
    assert config.get('desktop.preferred_browser') == 'firefox'       # the real setting follows the preference
    memory169.note(db, config, 'пароль от wifi у соседа', 'ru')
    listing = memory169.listing(db, config, 'ru')
    assert 'мой основной браузер — Firefox' in listing and 'wifi' in listing
    assert memory169.facts_for_prompt(db, config)
    ok, _ = memory169.forget(db, 'это предпочтение', 'ru')             # «это» -> the last one shown
    assert ok and 'Firefox' not in memory169.listing(db, config, 'ru')
    config.data['memory'] = {'enabled': False}
    assert memory169.remember(db, config, 'x', 'y', 'ru')[0] is False
    assert memory169.facts_for_prompt(db, config) == []
    config.data['memory'] = {'enabled': True}
    assert 'очищена' in memory169.clear(db, config, 'ru')
    assert memory169.listing(db, config, 'ru') == memory169.t('empty', 'ru')


def test_confirmed_correction_is_an_exact_rule_at_once(tmp_path):
    from jarvis.runtime import memory169
    config = Config(tmp_path)
    assert memory169.correction_rule(config, 'назад', 'ru') is None
    assert memory169.record_correction(config, 'ru', 'Назад!', ('music', {'action': 'previous'}), ('browser_nav', {'action': 'back'}))
    assert memory169.correction_rule(config, 'назад', 'ru') == ('browser_nav', {'action': 'back'})
    assert memory169.correction_rule(config, 'назад', 'en') is None
    config.data['memory'] = {'enabled': False}
    assert memory169.correction_rule(config, 'назад', 'ru') is None


def test_only_a_misunderstood_action_becomes_a_rule(tmp_path):
    from jarvis.agent import Agent
    from jarvis.agent_types import AgentTurn
    from jarvis.runtime import memory169
    config = Config(tmp_path)
    agent = SimpleNamespace(config=config, _last_command169=('назад', ('music', {'action': 'previous'})))
    Agent._remember_command169(agent, 'correction', 'предыдущую страницу', 'ru', 'browser_nav', {'action': 'back'}, AgentTurn('ok', success=True))
    assert memory169.correction_rule(config, 'назад', 'ru') == ('browser_nav', {'action': 'back'})
    agent._last_command169 = ('открой блокнот', ('application', {'action': 'open', 'name': 'блокнот'}))
    Agent._remember_command169(agent, 'correction', 'калькулятор', 'ru', 'application', {'action': 'open', 'name': 'калькулятор'}, AgentTurn('ok', success=True))
    assert memory169.correction_rule(config, 'открой блокнот', 'ru') is None      # a changed value is not a rule


# ---------------------------------------------------------- file search
def _pdf(path: Path, pages: list[str]) -> None:
    """A minimal text PDF (one text line per page), written by hand."""
    objects, kids = [], []
    objects.append('<< /Type /Catalog /Pages 2 0 R >>')
    objects.append(None)                                   # pages, filled below
    objects.append('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    for text in pages:
        stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'
        objects.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objects)} 0 R '
                       '/Resources << /Font << /F1 3 0 R >> >> >>')
        kids.append(len(objects))
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offsets = '%PDF-1.4\n', []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out.encode('latin-1')))
        out += f'{number} 0 obj\n{body}\nendobj\n'
    xref = len(out.encode('latin-1'))
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n' + ''.join(f'{o:010d} 00000 n \n' for o in offsets)
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'
    path.write_bytes(out.encode('latin-1'))


def test_file_search_cites_file_page_and_section_and_updates_incrementally(tmp_path):
    from jarvis.runtime.knowledge169 import KnowledgeIndex, cite
    docs = tmp_path / 'docs'
    docs.mkdir()
    (docs / 'trip.md').write_text('# Plan\nWe go to Kazan on August 12.\n\n# Budget\nWe saved 80 thousand.\n', encoding='utf-8')
    _pdf(docs / 'manual.pdf', ['Introduction to the device', 'Reset the router by holding the button'])
    (docs / 'ignored.exe').write_bytes(b'MZ')
    index = KnowledgeIndex(tmp_path / 'index.sqlite3')
    stats = index.update_folders([str(docs)], pause=0)
    assert stats['indexed'] == 2
    hits = index.search('reset router')
    assert hits and hits[0]['file'].endswith('manual.pdf') and hits[0]['location'] == 'p:2'
    assert 'manual.pdf, p. 2' in cite(hits[:1], 'en')
    assert index.search('budget saved')[0]['location'] == 'h:Budget'
    assert index.search('recipe for borscht') == []
    assert index.update_folders([str(docs)], pause=0)['indexed'] == 0                 # nothing changed -> nothing re-read
    (docs / 'trip.md').unlink()
    assert index.update_folders([str(docs)], pause=0)['removed'] == 1
    assert not any(h['file'].endswith('trip.md') for h in index.search('Kazan'))


def test_folders_outside_the_allowed_list_are_dropped(tmp_path):
    from jarvis.runtime.knowledge169 import KnowledgeIndex
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(), b.mkdir()
    (a / 'x.txt').write_text('alpha secret notes', encoding='utf-8')
    (b / 'y.txt').write_text('beta notes', encoding='utf-8')
    index = KnowledgeIndex(tmp_path / 'i.sqlite3')
    index.update_folders([str(a), str(b)], pause=0)
    index.update_folders([str(b)], pause=0)                   # the user removed folder a
    assert index.search('alpha') == [] and index.search('beta')


# ---------------------------------------------------------------- plugins
def _make_plugin(root: Path, name: str, permissions: list[str], body: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    manifest = {'name': name, 'version': '1.0', 'description': 'test plugin', 'tools': [{'name': name, 'description': 'x'}],
                'permissions': permissions, 'timeout_seconds': 1,
                'phrases': {f'{name}.run': ['Запусти тестовый плагин', 'Run the test plugin', 'Starte das Testplugin', 'Lance le plugin de test',
                                            'Ejecuta el plugin de prueba', 'Avvia il plugin di prova', 'Executa o plugin de teste']}}
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (folder / 'plugin.py').write_text(body, encoding='utf-8')
    return folder


_PLUGIN = '''
from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution
import time

def register_plugin(context):
    def handler(args):
        mode = args.get('mode', '')
        if mode == 'clipboard':
            return ToolExecution(True, context.clipboard_text(), {})
        if mode == 'sleep':
            time.sleep(3)
        if mode == 'crash':
            raise RuntimeError('boom')
        return ToolExecution(True, 'ok', {})
    context.register(ToolDefinition('{name}', 'test', {'type': 'object', 'properties': {'action': {'type': 'string'}, 'mode': {'type': 'string'}},
                                    'required': ['action']}, handler, lambda a: Risk.SAFE))
'''


class _Registry:
    def __init__(self, config):
        self.config, self.security, self._tools, self.tool_plugin_owners = config, None, {}, {}

    def register(self, tool):
        self._tools[tool.name] = tool

    def unregister(self, name):
        self.tool_plugin_owners.pop(name, None)
        return self._tools.pop(name, None) is not None

    def execute(self, name, args):
        return self._tools[name].handler(args)


def test_plugins_need_approval_and_reapproval_after_a_code_change(tmp_path):
    from jarvis.runtime.plugins import PluginManager
    config = Config(tmp_path / 'profile')
    root = tmp_path / 'plugins'
    folder = _make_plugin(root, 'demo', [], _PLUGIN.replace('{name}', 'demo'))
    (root / '_template').mkdir()
    registry = _Registry(config)
    manager = PluginManager(root, config)
    infos = manager.load_into(registry)
    assert [i.state for i in infos] == ['needs_approval'] and 'demo' not in registry._tools
    manager.approve('demo')
    manager.reload(registry)
    assert 'demo' in registry._tools and registry.tool_plugin_owners['demo'] == 'user.demo'
    (folder / 'plugin.py').write_text(_PLUGIN.replace('{name}', 'demo') + '\n# changed\n', encoding='utf-8')
    manager.reload(registry)
    assert 'demo' not in registry._tools and manager.info('demo').state == 'changed'


def test_plugin_permissions_are_checked_when_used_and_errors_are_isolated(tmp_path):
    from jarvis.runtime.plugins import PluginManager
    config = Config(tmp_path / 'profile')
    root = tmp_path / 'plugins'
    _make_plugin(root, 'nosy', [], _PLUGIN.replace('{name}', 'nosy'))
    registry = _Registry(config)
    manager = PluginManager(root, config)
    manager.discover()
    manager.approve('nosy')
    manager.reload(registry)
    denied = registry.execute('nosy', {'action': 'run', 'mode': 'clipboard'})
    assert denied.success is False and 'clipboard.read' in denied.text
    started = time.perf_counter()
    slow = registry.execute('nosy', {'action': 'run', 'mode': 'sleep'})
    assert slow.success is False and slow.data.get('timeout') and time.perf_counter() - started < 2.5
    registry.execute('nosy', {'action': 'run', 'mode': 'crash'})
    paused = registry.execute('nosy', {'action': 'run'})
    assert paused.data.get('paused')                  # three failures in a row pause the plugin


def test_plugin_phrases_join_the_grammar_and_leave_with_the_plugin(tmp_path):
    from jarvis.runtime import canonical_commands
    from jarvis.runtime.plugins import PluginManager
    config = Config(tmp_path / 'profile')
    root = tmp_path / 'plugins'
    _make_plugin(root, 'phrasy', [], _PLUGIN.replace('{name}', 'phrasy'))
    registry = _Registry(config)
    manager = PluginManager(root, config)
    manager.discover()
    manager.approve('phrasy')
    manager.reload(registry)
    schema = {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['run']}}, 'required': ['action']}
    try:
        match = canonical_commands.parse_canonical_command('Запусти тестовый плагин', 'ru', lambda name: schema)
        assert match is not None and match.tool == 'phrasy'
    finally:
        manager.set_enabled('phrasy', False)
        manager.reload(registry)
    assert canonical_commands.parse_canonical_command('Запусти тестовый плагин', 'ru', lambda name: schema) is None


def test_invalid_manifest_is_reported_not_loaded(tmp_path):
    from jarvis.runtime.plugins import read_manifest
    folder = tmp_path / 'bad'
    folder.mkdir()
    (folder / 'manifest.json').write_text(json.dumps({'name': 'bad', 'version': '1', 'description': 'x', 'tools': [{'name': 'bad'}],
                                                      'permissions': ['root_access']}), encoding='utf-8')
    assert 'unknown permission' in read_manifest(folder)[1]


def test_bundled_example_plugin_is_valid():
    from jarvis.runtime.plugins import read_manifest
    manifest, error = read_manifest(Path(__file__).resolve().parents[1] / 'plugins' / 'text_stats')
    assert not error and manifest['permissions'] == ['clipboard.read']
