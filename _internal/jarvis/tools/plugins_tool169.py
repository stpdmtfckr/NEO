"""NEO 169 tool ``plugins``: see, approve, turn off, remove and reload user plugins.

Turning a plugin on is two steps: «включи плагин X» shows what it does and which
permissions it needs; only «подтверждаю плагин X» (within two minutes) approves it.
"""
from __future__ import annotations

import time
from typing import Any

from jarvis.runtime.plugin_permissions169 import describe
from jarvis.runtime.plugins import PluginPreviewChanged
from jarvis.tools.registry import ToolExecution

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_PENDING: dict[str, tuple[float, tuple]] = {}
_STATE = {
    'loaded': ('включён', 'on', 'aktiv', 'activé', 'activo', 'attivo', 'ativo'),
    'ready': ('включён', 'on', 'aktiv', 'activé', 'activo', 'attivo', 'ativo'),
    'needs_approval': ('ждёт разрешения', 'waiting for approval', 'wartet auf Freigabe', 'en attente d’autorisation',
                       'pendiente de aprobación', 'in attesa di approvazione', 'a aguardar aprovação'),
    'changed': ('изменён — нужно разрешить заново', 'changed — approve again', 'geändert — neu freigeben', 'modifié — à réautoriser',
                'cambiado — apruébalo de nuevo', 'modificato — riapprovalo', 'alterado — aprove novamente'),
    'disabled': ('выключен', 'off', 'aus', 'désactivé', 'desactivado', 'disattivato', 'desativado'),
    'invalid': ('ошибка в описании', 'invalid manifest', 'ungültiges Manifest', 'manifeste invalide', 'manifiesto no válido', 'manifest non valido', 'manifesto inválido'),
    'failed': ('не запустился', 'failed to start', 'Start fehlgeschlagen', 'échec du démarrage', 'no se pudo iniciar', 'avvio non riuscito', 'falhou ao iniciar'),
}
_TEXT = {
    'none': ('Плагинов нет. Положите папку плагина в {folder} и скажите «обнови список плагинов».',
             'No plugins. Put a plugin folder into {folder} and say “reload plugins”.',
             'Keine Plugins. Legen Sie einen Plugin-Ordner in {folder} und sagen Sie „Plugins neu laden“.',
             'Aucun plugin. Placez un dossier de plugin dans {folder} et dites « recharge les plugins ».',
             'No hay plugins. Pon la carpeta del plugin en {folder} y di «recarga los plugins».',
             'Nessun plugin. Metti la cartella del plugin in {folder} e di’ «ricarica i plugin».',
             'Sem plugins. Coloque a pasta do plugin em {folder} e diga «recarrega os plugins».'),
    'list': ('Плагины:', 'Plugins:', 'Plugins:', 'Plugins :', 'Plugins:', 'Plugin:', 'Plugins:'),
    'not_found': ('Плагин «{name}» не найден.', 'Plugin “{name}” not found.', 'Plugin „{name}“ nicht gefunden.', 'Plugin « {name} » introuvable.',
                  'No encontré el plugin «{name}».', 'Plugin «{name}» non trovato.', 'Plugin «{name}» não encontrado.'),
    'describe': ('Плагин «{name}» {version}: {description}\nКоманды: {tools}\nРазрешения:\n{permissions}',
                 'Plugin “{name}” {version}: {description}\nCommands: {tools}\nPermissions:\n{permissions}',
                 'Plugin „{name}“ {version}: {description}\nBefehle: {tools}\nBerechtigungen:\n{permissions}',
                 'Plugin « {name} » {version} : {description}\nCommandes : {tools}\nAutorisations :\n{permissions}',
                 'Plugin «{name}» {version}: {description}\nComandos: {tools}\nPermisos:\n{permissions}',
                 'Plugin «{name}» {version}: {description}\nComandi: {tools}\nPermessi:\n{permissions}',
                 'Plugin «{name}» {version}: {description}\nComandos: {tools}\nPermissões:\n{permissions}'),
    'no_permissions': ('— никаких (только собственные вычисления)', '— none (own calculations only)', '— keine', '— aucune', '— ninguno', '— nessuno', '— nenhuma'),
    'confirm_hint': ('Если согласны, скажите «подтверждаю плагин {name}». Важно: плагин — это код, который работает внутри NEO; разрешения проверяются для функций NEO, но это не изолированная песочница. Включайте плагины только из источников, которым доверяете.',
                     'If you agree, say “approve plugin {name}”. Note: a plugin is code running inside NEO; permissions are checked for NEO’s services, but it is not an isolated sandbox. Only enable plugins you trust.',
                     'Wenn Sie zustimmen, sagen Sie „Plugin {name} bestätigen“. Ein Plugin ist Code innerhalb von NEO, keine isolierte Sandbox.',
                     'Si vous êtes d’accord, dites « confirme le plugin {name} ». Un plugin est du code exécuté dans NEO, pas un bac à sable isolé.',
                     'Si estás de acuerdo, di «confirmo el plugin {name}». Un plugin es código dentro de NEO, no un entorno aislado.',
                     'Se sei d’accordo, di’ «confermo il plugin {name}». Un plugin è codice dentro NEO, non una sandbox isolata.',
                     'Se concorda, diga «confirmo o plugin {name}». Um plugin é código dentro do NEO, não uma sandbox isolada.'),
    'show_first': ('Сначала скажите «включи плагин {name}» — я покажу, что он делает и какие разрешения просит.',
                   'First say “enable plugin {name}” so I can show what it does and which permissions it needs.',
                   'Sagen Sie zuerst „aktiviere Plugin {name}“.', 'Dites d’abord « active le plugin {name} ».',
                   'Primero di «activa el plugin {name}».', 'Prima di’ «attiva il plugin {name}».', 'Primeiro diga «ativa o plugin {name}».'),
    'preview_changed': ('Плагин «{name}» изменился после показа (версия, код или разрешения). Ниже — новые сведения; подтвердите ещё раз.',
                        'Plugin “{name}” changed after I showed it (version, code or permissions). Here is the new description; confirm again.',
                        'Plugin „{name}“ hat sich nach der Anzeige geändert (Version, Code oder Berechtigungen). Hier die neuen Angaben; bitte erneut bestätigen.',
                        'Le plugin « {name} » a changé depuis l’affichage (version, code ou autorisations). Voici la nouvelle description ; confirmez à nouveau.',
                        'El plugin «{name}» cambió después de mostrarlo (versión, código o permisos). Esta es la nueva descripción; confirma de nuevo.',
                        'Il plugin «{name}» è cambiato dopo che l’ho mostrato (versione, codice o permessi). Ecco la nuova descrizione; conferma di nuovo.',
                        'O plugin «{name}» mudou depois de o mostrar (versão, código ou permissões). Eis a nova descrição; confirme novamente.'),
    'approved': ('Плагин «{name}» включён. Его команды уже доступны.', 'Plugin “{name}” is on. Its commands are available.',
                 'Plugin „{name}“ ist aktiv.', 'Le plugin « {name} » est activé.', 'El plugin «{name}» está activo.',
                 'Il plugin «{name}» è attivo.', 'O plugin «{name}» está ativo.'),
    'failed_start': ('Плагин «{name}» разрешён, но не запустился: {error}', 'Plugin “{name}” was approved but failed to start: {error}',
                     'Plugin „{name}“ freigegeben, Start fehlgeschlagen: {error}', 'Plugin « {name} » autorisé mais n’a pas démarré : {error}',
                     'Plugin «{name}» aprobado, pero no se inició: {error}', 'Plugin «{name}» approvato ma non avviato: {error}',
                     'Plugin «{name}» aprovado, mas não arrancou: {error}'),
    'disabled': ('Плагин «{name}» выключен.', 'Plugin “{name}” is off.', 'Plugin „{name}“ ist aus.', 'Plugin « {name} » désactivé.',
                 'Plugin «{name}» desactivado.', 'Plugin «{name}» disattivato.', 'Plugin «{name}» desativado.'),
    'removed': ('Плагин «{name}» удалён (папка перенесена в {target}).', 'Plugin “{name}” removed (folder moved to {target}).',
                'Plugin „{name}“ entfernt (nach {target} verschoben).', 'Plugin « {name} » supprimé (déplacé vers {target}).',
                'Plugin «{name}» eliminado (movido a {target}).', 'Plugin «{name}» rimosso (spostato in {target}).',
                'Plugin «{name}» removido (movido para {target}).'),
    'reloaded': ('Список плагинов обновлён: включено {on}, ждут разрешения {waiting}.', 'Plugins reloaded: {on} on, {waiting} waiting for approval.',
                 'Plugins neu geladen: {on} aktiv, {waiting} warten.', 'Plugins rechargés : {on} actifs, {waiting} en attente.',
                 'Plugins recargados: {on} activos, {waiting} pendientes.', 'Plugin ricaricati: {on} attivi, {waiting} in attesa.',
                 'Plugins recarregados: {on} ativos, {waiting} pendentes.'),
    'invalid': ('Плагин «{name}» нельзя включить: {error}', 'Plugin “{name}” cannot be enabled: {error}', 'Plugin „{name}“ ungültig: {error}',
                'Plugin « {name} » invalide : {error}', 'Plugin «{name}» no válido: {error}', 'Plugin «{name}» non valido: {error}',
                'Plugin «{name}» inválido: {error}'),
}


def _t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_CODES.index(code) if code in _CODES else 1].format(**values)


def _state(state: str, code: str) -> str:
    row = _STATE.get(state)
    return row[_CODES.index(code) if code in _CODES else 1] if row else state


def _shown188(info: Any) -> tuple:
    return (str(info.fingerprint), str(info.version), tuple(info.permissions), tuple(info.tools))


def _preview_changed188(info: Any, code: str) -> ToolExecution:
    """Void the old consent and show (and arm) the new metadata instead."""
    permissions = '\n'.join('— ' + describe(p, code) for p in info.permissions) or _t('no_permissions', code)
    text = _t('preview_changed', code, name=info.name) + '\n\n' + _t(
        'describe', code, name=info.name, version=info.version, description=info.description,
        tools=', '.join(info.tools), permissions=permissions) + '\n\n' + _t('confirm_hint', code, name=info.name)
    _PENDING[info.name] = (time.monotonic(), _shown188(info))
    return ToolExecution(False, text, {'plugin': info.name, 'requires_clarification': True, 'preview_changed': True})


def run(config: Any, registry: Any, args: dict[str, Any]) -> ToolExecution:
    code = str(config.get('app.language', 'ru') or 'ru')
    manager = getattr(registry, 'plugin_manager', None)
    if manager is None:
        raise RuntimeError('plugin manager unavailable')
    action = str(args.get('action', '') or '')
    name = str(args.get('name', '') or '').strip().strip('«»"“”')
    if action == 'reload':
        infos = manager.reload(registry)
        on = sum(i.state == 'loaded' for i in infos)
        waiting = sum(i.state in {'needs_approval', 'changed'} for i in infos)
        return ToolExecution(True, _t('reloaded', code, on=on, waiting=waiting), {'plugins': manager.status()})
    if action == 'list':
        infos = manager.discover()
        for info in infos:        # a loaded plugin keeps its runtime state
            if info.name in manager._registered:
                info.state = 'loaded'
        if not infos:
            return ToolExecution(True, _t('none', code, folder=manager.root), {'plugins': []})
        lines = [_t('list', code)] + [f'• {i.name} {i.version} — {_state(i.state, code)}' for i in infos]
        return ToolExecution(True, '\n'.join(lines), {'plugins': manager.status()})
    info = manager.info(name)
    if info is None:
        return ToolExecution(False, _t('not_found', code, name=name), {'requires_clarification': True})
    if action in {'describe', 'enable'}:
        if info.state == 'invalid':
            return ToolExecution(False, _t('invalid', code, name=info.name, error=info.error), {})
        permissions = '\n'.join('— ' + describe(p, code) for p in info.permissions) or _t('no_permissions', code)
        text = _t('describe', code, name=info.name, version=info.version, description=info.description,
                  tools=', '.join(info.tools), permissions=permissions)
        if action == 'enable' and info.name not in manager._registered:
            # HIST-036: the consent belongs to exactly the metadata that was shown.
            _PENDING[info.name] = (time.monotonic(), _shown188(info))
            text += '\n\n' + _t('confirm_hint', code, name=info.name)
        return ToolExecution(True, text, {'plugin': info.name, 'permissions': info.permissions, 'state': info.state})
    if action == 'approve':
        pending = _PENDING.pop(info.name, None)
        shown_at, shown = pending if isinstance(pending, tuple) else (float(pending or 0.0), None)
        if time.monotonic() - shown_at > 120:
            return ToolExecution(False, _t('show_first', code, name=info.name), {'requires_clarification': True})
        manager.discover()
        info = manager.info(info.name) or info           # the plugin as it is now, not as listed earlier
        if shown is None or shown != _shown188(info):
            return _preview_changed188(info, code)
        try:
            manager.approve(info.name, expected=shown)
        except PluginPreviewChanged:
            return _preview_changed188(manager.info(info.name) or info, code)
        manager.reload(registry)
        loaded = manager.info(info.name)
        if loaded is None or loaded.name not in manager._registered:
            return ToolExecution(False, _t('failed_start', code, name=info.name, error=(loaded.error if loaded else '')), {})
        return ToolExecution(True, _t('approved', code, name=info.name), {'plugin': info.name, 'tools': loaded.tools})
    if action == 'disable':
        manager.set_enabled(info.name, False)
        manager.reload(registry)
        return ToolExecution(True, _t('disabled', code, name=info.name), {'plugin': info.name})
    if action == 'remove':
        target = manager.remove(info.name, registry)
        return ToolExecution(True, _t('removed', code, name=info.name, target=target), {'plugin': info.name, 'moved_to': str(target)})
    raise ValueError(f'unknown plugins action: {action}')
