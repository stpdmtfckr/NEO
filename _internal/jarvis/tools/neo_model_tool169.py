"""NEO 169 tool ``neo_model``: status, retraining, versions and rollback of the NEO command model."""
from __future__ import annotations

from typing import Any

from jarvis.tools.registry import ToolExecution

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_TEXT = {
    'status': ('Модель команд NEO: версия {version}. Точность на новых формулировках {acc}, на опечатках {typo}. Поправок для дообучения: {fixes}.',
               'NEO command model: version {version}. Accuracy on unseen phrasings {acc}, on typos {typo}. Corrections for retraining: {fixes}.',
               'NEO-Befehlsmodell: Version {version}. Genauigkeit bei neuen Formulierungen {acc}, bei Tippfehlern {typo}. Korrekturen: {fixes}.',
               'Modèle de commandes NEO : version {version}. Précision sur formulations nouvelles {acc}, sur fautes {typo}. Corrections : {fixes}.',
               'Modelo de comandos NEO: versión {version}. Precisión con frases nuevas {acc}, con erratas {typo}. Correcciones: {fixes}.',
               'Modello dei comandi NEO: versione {version}. Precisione su frasi nuove {acc}, su refusi {typo}. Correzioni: {fixes}.',
               'Modelo de comandos NEO: versão {version}. Precisão em frases novas {acc}, com erros {typo}. Correções: {fixes}.'),
    'none': ('Модель команд не установлена: работают правила и разговор.', 'The command model is not installed: rules and conversation still work.',
             'Das Befehlsmodell ist nicht installiert.', 'Le modèle de commandes n’est pas installé.', 'El modelo de comandos no está instalado.',
             'Il modello dei comandi non è installato.', 'O modelo de comandos não está instalado.'),
    'started': ('Начал дообучение ({version}) в фоне с низким приоритетом. Это займёт несколько минут; новая версия включится, только если окажется не хуже текущей.',
                'Retraining started ({version}) in the background at low priority. It takes a few minutes; the new version is used only if it is not worse.',
                'Nachtraining gestartet ({version}), im Hintergrund mit niedriger Priorität. Die neue Version wird nur genutzt, wenn sie nicht schlechter ist.',
                'Réentraînement lancé ({version}) en arrière-plan, priorité basse. La nouvelle version ne sera utilisée que si elle n’est pas moins bonne.',
                'Reentrenamiento iniciado ({version}) en segundo plano con baja prioridad. La nueva versión solo se usará si no es peor.',
                'Riaddestramento avviato ({version}) in background a bassa priorità. La nuova versione sarà usata solo se non peggiore.',
                'Retreino iniciado ({version}) em segundo plano com baixa prioridade. A nova versão só é usada se não for pior.'),
    'busy': ('Дообучение уже идёт ({version}).', 'Retraining is already running ({version}).', 'Nachtraining läuft bereits ({version}).',
             'Réentraînement déjà en cours ({version}).', 'El reentrenamiento ya está en marcha ({version}).', 'Riaddestramento già in corso ({version}).',
             'O retreino já está a decorrer ({version}).'),
    'promoted': ('Новая версия {version} проверена и включена.', 'The new version {version} passed the checks and is active.',
                 'Neue Version {version} geprüft und aktiv.', 'Nouvelle version {version} vérifiée et activée.', 'Nueva versión {version} comprobada y activa.',
                 'Nuova versione {version} verificata e attiva.', 'Nova versão {version} verificada e ativa.'),
    'rejected': ('Версия {version} оказалась хуже текущей ({problems}) и не включена; работает прежняя.',
                 'Version {version} was worse than the current one ({problems}) and was not activated.',
                 'Version {version} war schlechter ({problems}) und wurde nicht aktiviert.', 'La version {version} était moins bonne ({problems}) et n’est pas activée.',
                 'La versión {version} era peor ({problems}) y no se activó.', 'La versione {version} era peggiore ({problems}) e non è attivata.',
                 'A versão {version} era pior ({problems}) e não foi ativada.'),
    'rolled_back': ('Вернул модель команд к версии {version}.', 'The command model is back to version {version}.', 'Befehlsmodell auf Version {version} zurückgesetzt.',
                    'Modèle de commandes revenu à la version {version}.', 'Modelo de comandos devuelto a la versión {version}.',
                    'Modello dei comandi riportato alla versione {version}.', 'Modelo de comandos reposto na versão {version}.'),
    'no_rollback': ('Нет предыдущей версии для отката.', 'There is no previous version to go back to.', 'Keine frühere Version vorhanden.',
                    'Aucune version précédente.', 'No hay versión anterior.', 'Nessuna versione precedente.', 'Não há versão anterior.'),
    'versions': ('Версии модели команд:', 'Command model versions:', 'Versionen des Befehlsmodells:', 'Versions du modèle :', 'Versiones del modelo:',
                 'Versioni del modello:', 'Versões do modelo:'),
}


def _t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_CODES.index(code) if code in _CODES else 1].format(**values)


def _pct(value: Any) -> str:
    return f'{float(value) * 100:.1f}%' if isinstance(value, (int, float)) else '—'


def run(config: Any, args: dict[str, Any], notify: Any = None) -> ToolExecution:
    from jarvis.neo_model import jobs, store
    from jarvis.runtime.memory169 import load_corrections
    code = str(config.get('app.language', 'ru') or 'ru')
    action = str(args.get('action', '') or '')
    if action == 'status':
        folder = store.active_folder(config)
        if folder is None:
            return ToolExecution(True, _t('none', code), {})
        meta = next((v for v in store.versions(config) if v['path'] == str(folder)), {})
        metrics = meta.get('metrics', {})
        text = _t('status', code, version=folder.name, acc=_pct(metrics.get('test_accuracy')), typo=_pct(metrics.get('typo_accuracy')),
                  fixes=len(load_corrections(config)))
        result = jobs.last_result()
        if jobs.running():
            text += '\n' + _t('busy', code, version=jobs._JOB['version'])
        elif result:
            text += '\n' + (_t('promoted', code, version=result['version']) if result.get('promoted')
                            else _t('rejected', code, version=result['version'], problems='; '.join(result.get('problems', []))))
        return ToolExecution(True, text, {'active': folder.name, 'metrics': metrics, 'training': jobs.running()})
    if action == 'train':
        if jobs.running():
            return ToolExecution(True, _t('busy', code, version=jobs._JOB['version']), {'running': True})

        def done(result: dict[str, Any]) -> None:
            if notify is None:
                return
            notify(_t('promoted', code, version=result['version']) if result.get('promoted')
                   else _t('rejected', code, version=result['version'], problems='; '.join(result.get('problems', []))))
        version = jobs.start(config, on_done=done)
        return ToolExecution(True, _t('started', code, version=version), {'version': version})
    if action == 'rollback':
        version = store.rollback(config, str(args.get('version', '') or ''))
        if version is None:
            return ToolExecution(False, _t('no_rollback', code), {})
        from jarvis.neo_model import runtime
        runtime.unload()
        return ToolExecution(True, _t('rolled_back', code, version=version), {'active': version})
    if action == 'versions':
        active = store.active_folder(config)
        lines = [_t('versions', code)]
        for row in store.versions(config):
            mark = '●' if active is not None and row['path'] == str(active) else '○'
            lines.append(f"{mark} {row['version']} ({row['origin']}) — {_pct(row['metrics'].get('test_accuracy'))}")
        return ToolExecution(True, '\n'.join(lines), {'versions': store.versions(config)})
    raise ValueError(f'unknown neo_model action: {action}')
