"""NEO 169: services a plugin may use, each checked against the permissions the user approved.

This is a permission layer, not a sandbox: a plugin runs as Python code inside
the NEO process. The checks below guard every operation offered through the
plugin context (files, network, clipboard, calling other NEO tools, settings),
and NEO only loads plugins the user approved after seeing their permissions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PERMISSIONS = {
    'files.read': 'Read files the user names',
    'files.write': 'Create or change files the user names (system folders stay protected)',
    'network': 'Connect to the internet',
    'clipboard.read': 'Read the clipboard',
    'clipboard.write': 'Put text on the clipboard',
    'notifications': 'Show notifications in NEO',
    'settings': 'Keep its own settings',
}
# «tools:<name>» lets a plugin call one existing NEO tool (with NEO's normal checks); «tools:*» any tool.

# HIST-082: one row per language (ru, en, de, fr, es, it, pt).
PERMISSION_TEXT = {
    'files.read': ('чтение файлов, которые вы назовёте', 'read files you name', 'Dateien lesen, die Sie nennen',
                   'lire les fichiers que vous indiquez', 'leer los archivos que indiques', 'leggere i file che indichi',
                   'ler os ficheiros que indicar'),
    'files.write': ('создание и изменение файлов, которые вы назовёте (системные папки защищены)',
                    'create or change files you name (system folders protected)',
                    'Dateien erstellen oder ändern, die Sie nennen (Systemordner bleiben geschützt)',
                    'créer ou modifier les fichiers que vous indiquez (dossiers système protégés)',
                    'crear o cambiar los archivos que indiques (las carpetas del sistema están protegidas)',
                    'creare o modificare i file che indichi (cartelle di sistema protette)',
                    'criar ou alterar os ficheiros que indicar (pastas do sistema protegidas)'),
    'network': ('доступ в интернет', 'internet access', 'Internetzugang', 'accès à Internet', 'acceso a internet',
                'accesso a internet', 'acesso à internet'),
    'clipboard.read': ('чтение буфера обмена', 'read the clipboard', 'Zwischenablage lesen', 'lire le presse-papiers',
                       'leer el portapapeles', 'leggere gli appunti', 'ler a área de transferência'),
    'clipboard.write': ('запись в буфер обмена', 'write to the clipboard', 'in die Zwischenablage schreiben',
                        'écrire dans le presse-papiers', 'escribir en el portapapeles', 'scrivere negli appunti',
                        'escrever na área de transferência'),
    'notifications': ('уведомления в NEO', 'notifications in NEO', 'Benachrichtigungen in NEO', 'notifications dans NEO',
                      'notificaciones en NEO', 'notifiche in NEO', 'notificações no NEO'),
    'settings': ('свои настройки', 'its own settings', 'eigene Einstellungen', 'ses propres réglages', 'sus propios ajustes',
                 'le proprie impostazioni', 'as suas próprias definições'),
}
_TOOLS_TEXT = {
    'ru': ('вызов команд NEO: ', 'все'), 'en': ('use NEO commands: ', 'all'), 'de': ('NEO-Befehle verwenden: ', 'alle'),
    'fr': ('utiliser les commandes NEO : ', 'toutes'), 'es': ('usar comandos de NEO: ', 'todos'),
    'it': ('usare i comandi di NEO: ', 'tutti'), 'pt': ('usar comandos do NEO: ', 'todos'),
}
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


class PluginPermissionError(PermissionError):
    pass


def valid_permission(value: str) -> bool:
    return value in PERMISSIONS or (value.startswith('tools:') and len(value) > 6)


def describe(permission: str, code: str) -> str:
    code = code if code in _CODES else 'en'
    if permission.startswith('tools:'):
        tool = permission[6:]
        prefix, every = _TOOLS_TEXT[code]
        return prefix + (every if tool == '*' else tool)
    row = PERMISSION_TEXT.get(permission)
    return row[_CODES.index(code)] if row else permission


class PluginServices:
    """What a plugin gets as ``context``: registry-like calls plus checked services."""

    def __init__(self, name: str, permissions: set[str], registry: Any, declared_tools: set[str]) -> None:
        self.plugin_name = name
        self.permissions = frozenset(permissions)
        self._registry = registry
        self._declared = frozenset(declared_tools)
        self.config = _PluginConfig(registry.config, name, 'settings' in permissions)
        self.registered: list[str] = []

    # -------------------------------------------------------------- checks
    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise PluginPermissionError(f'Plugin «{self.plugin_name}» has no permission «{permission}»')

    # ------------------------------------------------------------ registry
    @property
    def registry(self) -> 'PluginServices':
        return self            # legacy plugins call context.registry.register(...)

    def register(self, tool: Any) -> None:
        name = str(getattr(tool, 'name', ''))
        if name not in self._declared:
            raise PluginPermissionError(f'Tool «{name}» is not declared in the manifest of «{self.plugin_name}»')
        self._registry.register(tool)
        self.registered.append(name)

    def execute(self, tool: str, arguments: dict[str, Any]) -> Any:
        if f'tools:{tool}' not in self.permissions and 'tools:*' not in self.permissions:
            raise PluginPermissionError(f'Plugin «{self.plugin_name}» may not call «{tool}»')
        return self._registry.execute(tool, arguments)   # NEO's own validation, risk and confirmation apply

    # ---------------------------------------------------------------- files
    def read_text(self, path: str | Path, *, limit: int = 2_000_000) -> str:
        self.require('files.read')
        target = Path(path).expanduser().resolve(strict=True)
        if target.stat().st_size > limit:
            raise ValueError('file is too large')
        return target.read_text(encoding='utf-8', errors='replace')

    def write_text(self, path: str | Path, text: str) -> Path:
        self.require('files.write')
        target = self._registry.security.assert_mutation_path_allowed(Path(path))
        target.write_text(str(text), encoding='utf-8')
        return target

    # -------------------------------------------------------------- network
    def http_get(self, url: str, *, timeout: float = 10.0) -> str:
        self.require('network')
        import requests
        response = requests.get(url, timeout=min(30.0, float(timeout)))
        response.raise_for_status()
        return response.text[:2_000_000]

    # ------------------------------------------------------------ clipboard
    def clipboard_text(self) -> str:
        self.require('clipboard.read')
        from jarvis.runtime import clipboard168
        content = clipboard168.read()
        return content.text if content.kind == 'text' else ''

    def set_clipboard_text(self, text: str) -> None:
        self.require('clipboard.write')
        from jarvis.runtime import clipboard168
        clipboard168.set_text(str(text))

    # -------------------------------------------------------- notifications
    def notify(self, text: str) -> None:
        self.require('notifications')
        try:
            from jarvis.ui.desktop_events import request_desktop
            request_desktop('notify', text=f'{self.plugin_name}: {str(text)[:300]}')
        except Exception:
            pass


class _PluginConfig:
    """Read NEO settings; write only ``plugins.settings.<name>`` (needs «settings»)."""

    def __init__(self, config: Any, name: str, writable: bool) -> None:
        self._config, self._name, self._writable = config, name, writable

    def get(self, key: str, default: Any = None) -> Any:
        if key.startswith(('telegram', 'discord', 'email', 'google', 'integrations', 'api', 'secrets')) or 'token' in key or 'key' in key.split('.')[-1]:
            return default     # plugins never read credentials of other integrations
        return self._config.get(key, default)

    def own(self, key: str, default: Any = None) -> Any:
        return self._config.get(f'plugins.settings.{self._name}.{key}', default)

    def set_own(self, key: str, value: Any) -> None:
        if not self._writable:
            raise PluginPermissionError(f'Plugin «{self._name}» has no permission «settings»')
        data = json.loads(json.dumps(self._config.data))
        data.setdefault('plugins', {}).setdefault('settings', {}).setdefault(self._name, {})[key] = value
        self._config.save(data)
