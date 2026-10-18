# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_KEYS = {'name', 'version', 'description', 'entry', 'tools', 'permissions', 'phrases', 'settings', 'tests',
                 'timeout_seconds', 'enabled', 'min_neo'}
# One small shared pool: a slow plugin can never block the UI or a voice command.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='neo-plugin')


@dataclass
class PluginInfo:
    name: str
    path: str
    enabled: bool
    loaded: bool = False
    error: str = ''
    version: str = ''
    description: str = ''
    permissions: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    state: str = ''            # loaded | needs_approval | changed | disabled | invalid | failed
    fingerprint: str = ''


def fingerprint(folder: Path) -> str:
    """Hash of the plugin's code and manifest: an approval is bound to this exact version."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py', '.json'}:
            digest.update(path.relative_to(folder).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:32]


def read_manifest(folder: Path) -> tuple[dict[str, Any], str]:
    """(manifest, error). Validates the fields NEO relies on."""
    from jarvis.runtime.plugin_permissions169 import valid_permission
    path = folder / 'manifest.json'
    if not path.exists():
        return {}, 'manifest.json is missing'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        return {}, f'manifest.json: {exc}'
    if not isinstance(data, dict):
        return {}, 'manifest.json must be an object'
    for key in ('name', 'version', 'description', 'tools', 'permissions'):
        if key not in data:
            return {}, f'manifest.json: «{key}» is required'
    tools = data.get('tools')
    if not isinstance(tools, list) or not all(isinstance(t, dict) and t.get('name') for t in tools):
        return {}, 'manifest.json: «tools» must be a list of {name, description}'
    permissions = data.get('permissions')
    if not isinstance(permissions, list) or not all(isinstance(p, str) and valid_permission(p) for p in permissions):
        return {}, 'manifest.json: unknown permission'
    phrases = data.get('phrases', {})
    if not isinstance(phrases, dict) or not all(isinstance(v, list) and len(v) == 7 for v in phrases.values()):
        return {}, 'manifest.json: «phrases» must map intent -> 7 phrases (ru, en, de, fr, es, it, pt)'
    declared = {t['name'] for t in tools}
    if any(intent.split('.', 1)[0] not in declared for intent in phrases):
        return {}, 'manifest.json: a phrase points to a tool the plugin does not declare'
    return data, ''


class PluginPreviewChanged(ValueError):
    """The plugin changed between the consent preview and the approval."""


_DISABLED188: set[str] = set()      # HIST-035: plugins disabled in this process, for every registry


class PluginManager:
    """Loads user plugins from ``plugins/<name>/`` — only the ones the user approved.

    A plugin folder contains ``manifest.json`` (name, version, description, tools,
    permissions, phrases, settings, tests) and ``plugin.py`` with
    ``register_plugin(context)``. The approval (settings ``plugins.approved``) stores
    the version, the permissions and a fingerprint of the code; any change to the
    code or a new permission requires approving again. Folders starting with «_»
    or «.» (templates, removed plugins) are ignored. Adding a plugin needs no
    rebuild of NEO: «обнови список плагинов» or a restart loads it.
    """

    def __init__(self, root: Path, config: Any | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.infos: list[PluginInfo] = []
        self._registered: dict[str, list[str]] = {}
        self._phrase_packs: dict[str, Path] = {}
        self._failures: dict[str, int] = {}

    # ------------------------------------------------------------ discovery
    def _approved(self) -> dict[str, dict[str, Any]]:
        value = self.config.get('plugins.approved', {}) if self.config is not None else {}
        return value if isinstance(value, dict) else {}

    def discover(self) -> list[PluginInfo]:
        found: list[PluginInfo] = []
        approved = self._approved()
        for folder in sorted(self.root.iterdir() if self.root.exists() else []):
            if not folder.is_dir() or folder.name.startswith(('_', '.')) or not (folder / 'plugin.py').exists():
                continue
            manifest, error = read_manifest(folder)
            name = str(manifest.get('name') or folder.name)
            info = PluginInfo(name, str(folder / 'plugin.py'), False, error=error, version=str(manifest.get('version', '')),
                              description=str(manifest.get('description', '')),
                              permissions=[str(p) for p in manifest.get('permissions', [])],
                              tools=[str(t['name']) for t in manifest.get('tools', [])])
            if error:
                info.state = 'invalid'
                found.append(info)
                continue
            info.fingerprint = fingerprint(folder)
            grant = approved.get(name)
            if not grant:
                info.state = 'needs_approval'
            elif not grant.get('enabled', True):
                info.state = 'disabled'
            elif grant.get('fingerprint') != info.fingerprint or not set(info.permissions) <= set(grant.get('permissions', [])):
                info.state = 'changed'
            else:
                info.state, info.enabled = 'ready', True
            found.append(info)
        self.infos = found
        return list(found)

    def info(self, name: str) -> PluginInfo | None:
        wanted = str(name or '').casefold().strip()
        for info in self.infos or self.discover():
            if info.name.casefold() == wanted or Path(info.path).parent.name.casefold() == wanted:
                return info
        return None

    # --------------------------------------------------------------- loading
    def load_into(self, registry: Any) -> list[PluginInfo]:
        infos = self.discover()
        for info in infos:
            if info.enabled and info.name not in self._registered:
                self._load(info, registry)
        return infos

    def _load(self, info: PluginInfo, registry: Any) -> None:
        from jarvis.runtime.plugin_permissions169 import PluginServices
        folder = Path(info.path).parent
        manifest, _ = read_manifest(folder)
        services = PluginServices(info.name, set(info.permissions), _GuardedRegistry(registry, self, info.name, manifest),
                                  set(info.tools))
        try:
            module_name = f'neo_plugin_{folder.name}_{info.fingerprint[:8]}'
            spec = importlib.util.spec_from_file_location(module_name, folder / 'plugin.py')
            if spec is None or spec.loader is None:
                raise RuntimeError('cannot import plugin.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            register_plugin = getattr(module, 'register_plugin', None)
            if not callable(register_plugin):
                raise RuntimeError('plugin.py must define register_plugin(context)')
            register_plugin(services)
            info.loaded, info.state = True, 'loaded'
            self._registered[info.name] = list(services.registered)
            if manifest.get('phrases'):
                pack = self._phrase_pack(info.name, manifest['phrases'])
                from jarvis.runtime import canonical_commands
                canonical_commands.register_extra_pack(pack)
                self._phrase_packs[info.name] = pack
        except Exception as exc:
            info.loaded, info.state, info.error = False, 'failed', f'{type(exc).__name__}: {exc}'
            for tool in services.registered:          # never leave half a plugin behind
                registry.unregister(tool)
            logger.exception('Plugin failed: %s', info.path)

    def _phrase_pack(self, name: str, phrases: dict[str, list[str]]) -> Path:
        base = Path(self.config.paths.root) if self.config is not None and hasattr(self.config, 'paths') else self.root
        path = base / 'plugin_phrases' / f'{name}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(phrases, ensure_ascii=False, indent=1), encoding='utf-8')
        return path

    def unload(self, name: str, registry: Any) -> None:
        for tool in self._registered.pop(name, []):
            registry.unregister(tool)
        pack = self._phrase_packs.pop(name, None)
        if pack is not None:
            from jarvis.runtime import canonical_commands
            canonical_commands.unregister_extra_pack(pack)

    def reload(self, registry: Any) -> list[PluginInfo]:
        """Pick up new/approved plugins and drop disabled/changed ones — no restart needed."""
        infos = self.discover()
        wanted = {i.name for i in infos if i.enabled}
        for name in list(self._registered):
            if name not in wanted:
                self.unload(name, registry)
        for info in infos:
            if info.enabled and info.name not in self._registered:
                self._load(info, registry)
            elif info.name in self._registered:
                info.loaded, info.state = True, 'loaded'
        return infos

    # ------------------------------------------------------- live policy
    def grant_active(self, name: str) -> bool:
        """HIST-035: is this plugin still approved and enabled right now?

        Reads the newest saved settings (another manager or window may have changed
        them) plus the process-wide disable set; never imports or runs plugin code."""
        if name in _DISABLED188:
            return False
        config = self.config
        if config is None:
            return True
        refresh = getattr(config, 'reload_if_changed', None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass
        grant = ((config.get('plugins.approved', {}) or {}) if hasattr(config, 'get') else {}).get(name)
        return isinstance(grant, dict) and bool(grant.get('enabled', True))

    # ---------------------------------------------------------- approvals
    def approve(self, name: str, *, expected: tuple | None = None) -> PluginInfo:
        self.discover()                  # HIST-036: approve what is on disk now, never a cached listing
        info = self.info(name)
        if info is None or info.state == 'invalid':
            raise ValueError('plugin not found or invalid')
        if expected is not None and tuple(expected) != (str(info.fingerprint), str(info.version),
                                                         tuple(info.permissions), tuple(info.tools)):
            # HIST-036: what is stored must be exactly what the user saw.
            raise PluginPreviewChanged(info.name)
        data = json.loads(json.dumps(self.config.data))
        data.setdefault('plugins', {}).setdefault('approved', {})[info.name] = {
            'version': info.version, 'permissions': list(info.permissions), 'fingerprint': info.fingerprint,
            'enabled': True, 'approved_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
        self.config.save(data)
        _DISABLED188.discard(info.name)
        return info

    def set_enabled(self, name: str, enabled: bool) -> PluginInfo:
        info = self.info(name)
        if info is None:
            raise ValueError('plugin not found')
        data = json.loads(json.dumps(self.config.data))
        grant = data.setdefault('plugins', {}).setdefault('approved', {}).get(info.name)
        if grant is None:
            raise ValueError('plugin was never approved')
        grant['enabled'] = bool(enabled)
        self.config.save(data)
        if enabled:
            _DISABLED188.discard(info.name)
        else:
            _DISABLED188.add(info.name)        # every live registry in this process refuses it at once
        return info

    def remove(self, name: str, registry: Any) -> Path:
        """Move the plugin folder to plugins/.removed (can be restored by hand) and forget its approval."""
        info = self.info(name)
        if info is None:
            raise ValueError('plugin not found')
        self.unload(info.name, registry)
        folder = Path(info.path).parent
        target = self.root / '.removed' / f'{folder.name}-{time.strftime("%Y%m%d-%H%M%S")}'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(target))
        data = json.loads(json.dumps(self.config.data))
        data.setdefault('plugins', {}).setdefault('approved', {}).pop(info.name, None)
        self.config.save(data)
        _DISABLED188.add(info.name)
        self.discover()
        return target

    # ----------------------------------------------------------- failures
    def record_failure(self, name: str) -> int:
        self._failures[name] = self._failures.get(name, 0) + 1
        return self._failures[name]

    def record_success(self, name: str) -> None:
        self._failures.pop(name, None)

    def status(self) -> list[dict[str, Any]]:
        return [asdict(x) for x in self.infos or self.discover()]


_GUARD_TEXT = {
    'disabled': ('Плагин «{p}» выключен, его команда не выполнена.', 'Plugin “{p}” is disabled; its command was not run.',
                 'Plugin „{p}“ ist deaktiviert; der Befehl wurde nicht ausgeführt.', 'Le plugin « {p} » est désactivé ; sa commande n’a pas été exécutée.',
                 'El plugin «{p}» está desactivado; su comando no se ejecutó.', 'Il plugin «{p}» è disattivato; il comando non è stato eseguito.',
                 'O plugin «{p}» está desativado; o comando não foi executado.'),
    'paused': ('Плагин «{p}» приостановлен после повторных ошибок. Скажите «обнови список плагинов», чтобы попробовать снова.',
               'Plugin “{p}” was paused after repeated errors. Say “reload plugins” to try again.',
               'Plugin „{p}“ nach wiederholten Fehlern pausiert. Sagen Sie „Plugins neu laden“.',
               'Plugin « {p} » suspendu après des erreurs répétées. Dites « recharge les plugins ».',
               'Plugin «{p}» en pausa tras errores repetidos. Di «recarga los plugins».',
               'Plugin «{p}» sospeso dopo errori ripetuti. Di’ «ricarica i plugin».',
               'Plugin «{p}» pausado após erros repetidos. Diga «recarrega os plugins».'),
    'timeout': ('Плагин «{p}» не ответил за {s} с.', 'Plugin “{p}” did not answer within {s} s.', 'Plugin „{p}“ antwortete nicht in {s} s.',
                'Le plugin « {p} » n’a pas répondu en {s} s.', 'El plugin «{p}» no respondió en {s} s.', 'Il plugin «{p}» non ha risposto in {s} s.',
                'O plugin «{p}» não respondeu em {s} s.'),
    'failed': ('Ошибка плагина «{p}»: {e}', 'Plugin “{p}” failed: {e}', 'Fehler im Plugin „{p}“: {e}', 'Erreur du plugin « {p} » : {e}',
               'Error del plugin «{p}»: {e}', 'Errore del plugin «{p}»: {e}', 'Erro do plugin «{p}»: {e}'),
}
_GUARD_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _guard_text(key: str, config: Any, **values: Any) -> str:
    code = str(config.get('app.language', 'en') or 'en') if config is not None else 'en'
    return _GUARD_TEXT[key][_GUARD_CODES.index(code) if code in _GUARD_CODES else 1].format(**values)


class _GuardedRegistry:
    """The registry as a plugin sees it: tools it declared, run with a timeout and error isolation."""

    def __init__(self, registry: Any, manager: PluginManager, plugin: str, manifest: dict[str, Any]) -> None:
        self._registry, self._manager, self._plugin = registry, manager, plugin
        self.config, self.security = registry.config, registry.security
        self._timeout = max(1.0, min(60.0, float(manifest.get('timeout_seconds', 10) or 10)))

    def register(self, tool: Any) -> None:
        from dataclasses import replace
        from jarvis.tools.registry import ToolExecution
        handler, plugin, manager, timeout, config = tool.handler, self._plugin, self._manager, self._timeout, self.config

        def guarded(arguments: dict[str, Any]) -> Any:
            # HIST-035: the live policy is checked at the call boundary, so a plugin
            # disabled through one registry/manager is refused by every other one.
            if not manager.grant_active(plugin):
                return ToolExecution(False, _guard_text('disabled', config, p=plugin), {'plugin': plugin, 'disabled': True})
            if manager._failures.get(plugin, 0) >= 3:
                return ToolExecution(False, _guard_text('paused', config, p=plugin), {'plugin': plugin, 'paused': True})
            future = _POOL.submit(handler, arguments)
            try:
                result = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                future.cancel()
                manager.record_failure(plugin)
                return ToolExecution(False, _guard_text('timeout', config, p=plugin, s=f'{timeout:.0f}'), {'plugin': plugin, 'timeout': True})
            except Exception as exc:
                manager.record_failure(plugin)
                logger.warning('Plugin %s failed: %s', plugin, exc)
                return ToolExecution(False, _guard_text('failed', config, p=plugin, e=f'{type(exc).__name__}: {str(exc)[:200]}'), {'plugin': plugin})
            manager.record_success(plugin)
            return result

        self._registry.register(replace(tool, handler=guarded))
        owners = getattr(self._registry, 'tool_plugin_owners', None)
        if isinstance(owners, dict):
            owners[tool.name] = 'user.' + plugin

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._registry.execute(*args, **kwargs)
