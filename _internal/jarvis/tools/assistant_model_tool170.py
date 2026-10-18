"""NEO 170 tool ``assistant_model``: the small agent model — status, install, fine-tuning, stop, rollback."""
from __future__ import annotations

from typing import Any

from jarvis.tools.registry import ToolExecution

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_TEXT = {
    'model': ('Модель ассистента: {model} ({size}).', 'Assistant model: {model} ({size}).', 'Assistenzmodell: {model} ({size}).',
              'Modèle d’assistant : {model} ({size}).', 'Modelo del asistente: {model} ({size}).', 'Modello assistente: {model} ({size}).',
              'Modelo do assistente: {model} ({size}).'),
    'missing': ('Модель ассистента ещё не установлена. Скажите «установи модель ассистента».',
                'The assistant model is not installed yet. Say “install the assistant model”.',
                'Das Assistenzmodell ist noch nicht installiert. Sag „installiere das Assistenzmodell“.',
                'Le modèle d’assistant n’est pas encore installé. Dites « installe le modèle d’assistant ».',
                'El modelo del asistente aún no está instalado. Di «instala el modelo del asistente».',
                'Il modello assistente non è ancora installato. Di «installa il modello assistente».',
                'O modelo do assistente ainda não está instalado. Diz «instala o modelo do assistente».'),
    'training': ('Дообучение: этап «{stage}»{progress}.', 'Fine-tuning: stage “{stage}”{progress}.', 'Nachtraining: Schritt „{stage}“{progress}.',
                 'Affinage : étape « {stage} »{progress}.', 'Ajuste: fase «{stage}»{progress}.', 'Affinamento: fase «{stage}»{progress}.',
                 'Afinação: etapa «{stage}»{progress}.'),
    'paused': (' (на паузе, пока вы работаете)', ' (paused while you use the computer)', ' (pausiert, solange Sie arbeiten)',
               ' (en pause pendant que vous travaillez)', ' (en pausa mientras usas el equipo)', ' (in pausa mentre lavori)',
               ' (em pausa enquanto usa o computador)'),
    'report': ('Последняя проверка: команды {cand} против {cur} у прежней модели, разговор {chat}. {verdict}',
               'Last check: commands {cand} vs {cur} for the previous model, conversation {chat}. {verdict}',
               'Letzte Prüfung: Befehle {cand} gegenüber {cur}, Gespräch {chat}. {verdict}',
               'Dernier contrôle : commandes {cand} contre {cur}, conversation {chat}. {verdict}',
               'Última comprobación: comandos {cand} frente a {cur}, conversación {chat}. {verdict}',
               'Ultimo controllo: comandi {cand} contro {cur}, conversazione {chat}. {verdict}',
               'Última verificação: comandos {cand} contra {cur}, conversa {chat}. {verdict}'),
    'promoted_earlier': ('Тогда новая версия {candidate} была включена; сейчас выбрана {model}.',
                         'Back then the new version {candidate} was activated; {model} is selected now.',
                         'Damals wurde die neue Version {candidate} aktiviert; jetzt ist {model} ausgewählt.',
                         'La nouvelle version {candidate} avait alors été activée ; {model} est sélectionnée maintenant.',
                         'Entonces se activó la nueva versión {candidate}; ahora está seleccionada {model}.',
                         'Allora era stata attivata la nuova versione {candidate}; ora è selezionata {model}.',
                         'Na altura a nova versão {candidate} foi ativada; agora está selecionada {model}.'),
    'promoted': ('Новая версия включена.', 'The new version is in use.', 'Die neue Version ist aktiv.', 'La nouvelle version est utilisée.',
                 'La nueva versión está en uso.', 'La nuova versione è in uso.', 'A nova versão está em uso.'),
    'rejected': ('Новая версия не лучше — оставлена прежняя.', 'The new version was not better — the previous one stays.',
                 'Die neue Version war nicht besser – die bisherige bleibt.', 'La nouvelle version n’était pas meilleure — l’ancienne reste.',
                 'La nueva versión no era mejor: se mantiene la anterior.', 'La nuova versione non era migliore: resta la precedente.',
                 'A nova versão não era melhor — fica a anterior.'),
    'never': ('Дообучение ещё не запускалось.', 'Fine-tuning has not run yet.', 'Nachtraining lief noch nicht.', 'L’affinage n’a pas encore eu lieu.',
              'El ajuste aún no se ha ejecutado.', 'L’affinamento non è ancora stato eseguito.', 'A afinação ainda não foi executada.'),
    'waiting_setup': (
        'Автоматическое дообучение ждёт локальные инструменты и веса выбранной модели. Чтобы явно установить их и начать обучение, скажите «дообучи модель ассистента». При первом запуске будут скачаны инструменты и веса.',
        'Automatic fine-tuning is waiting for local tools and the selected model weights. To install them and train explicitly, say “fine-tune the assistant model”. The first run downloads tools and weights.',
        'Automatisches Nachtraining wartet auf lokale Werkzeuge und die Gewichte des gewählten Modells. Fordere das Nachtraining des Assistenzmodells ausdrücklich an, um sie zu installieren und zu trainieren. Beim ersten Lauf werden Werkzeuge und Gewichte heruntergeladen.',
        'L’affinage automatique attend les outils locaux et les poids du modèle choisi. Demandez explicitement l’affinage du modèle d’assistant pour les installer et lancer l’entraînement. Le premier lancement télécharge les outils et les poids.',
        'El ajuste automático espera las herramientas locales y los pesos del modelo elegido. Solicita explícitamente ajustar el modelo del asistente para instalarlos y entrenarlo. La primera ejecución descarga las herramientas y los pesos.',
        'L’affinamento automatico attende gli strumenti locali e i pesi del modello scelto. Richiedi esplicitamente l’affinamento del modello assistente per installarli e avviare l’addestramento. La prima esecuzione scarica strumenti e pesi.',
        'A afinação automática aguarda as ferramentas locais e os pesos do modelo escolhido. Pede explicitamente a afinação do modelo do assistente para os instalar e iniciar o treino. A primeira execução descarrega as ferramentas e os pesos.'),
    'waiting_local': (
        'Для автоматического дообучения укажите локальный адрес Ollama (localhost или 127.0.0.1) в настройках разработчика. Примеры обучения не отправляются на удалённый сервер автоматически.',
        'For automatic fine-tuning, select a local Ollama address (localhost or 127.0.0.1) in developer settings. Training examples are not sent to a remote server automatically.',
        'Wähle für automatisches Nachtraining eine lokale Ollama-Adresse (localhost oder 127.0.0.1) in den Entwicklereinstellungen. Trainingsbeispiele werden nicht automatisch an einen entfernten Server gesendet.',
        'Pour l’affinage automatique, choisissez une adresse Ollama locale (localhost ou 127.0.0.1) dans les paramètres développeur. Les exemples d’entraînement ne sont pas envoyés automatiquement à un serveur distant.',
        'Para el ajuste automático, elige una dirección local de Ollama (localhost o 127.0.0.1) en los ajustes de desarrollador. Los ejemplos de entrenamiento no se envían automáticamente a un servidor remoto.',
        'Per l’affinamento automatico, scegli un indirizzo Ollama locale (localhost o 127.0.0.1) nelle impostazioni sviluppatore. Gli esempi di addestramento non vengono inviati automaticamente a un server remoto.',
        'Para a afinação automática, escolhe um endereço Ollama local (localhost ou 127.0.0.1) nas definições de programador. Os exemplos de treino não são enviados automaticamente para um servidor remoto.'),
    'started': ('Начал дообучение модели ассистента на командах NEO. Оно идёт в фоне с низким приоритетом и ставится на паузу, пока вы работаете или играете; '
                'в первый раз скачиваются инструменты обучения и исходные веса. Новая версия включится, только если окажется лучше.',
                'Fine-tuning of the assistant model on NEO’s commands has started. It runs in the background at low priority and pauses while you work or play; '
                'the first time it downloads the training tools and the base weights. The new version is used only if it is better.',
                'Nachtraining des Assistenzmodells gestartet: im Hintergrund, niedrige Priorität, pausiert während Sie arbeiten oder spielen. '
                'Die neue Version wird nur genutzt, wenn sie besser ist.',
                'Affinage du modèle d’assistant lancé : en arrière-plan, priorité basse, en pause pendant que vous travaillez ou jouez. '
                'La nouvelle version ne sera utilisée que si elle est meilleure.',
                'Ajuste del modelo del asistente iniciado: en segundo plano, baja prioridad, en pausa mientras trabajas o juegas. '
                'La nueva versión solo se usará si es mejor.',
                'Affinamento del modello assistente avviato: in background, bassa priorità, in pausa mentre lavori o giochi. '
                'La nuova versione sarà usata solo se migliore.',
                'Afinação do modelo do assistente iniciada: em segundo plano, baixa prioridade, em pausa enquanto trabalha ou joga. '
                'A nova versão só é usada se for melhor.'),
    'busy': ('Дообучение уже идёт.', 'Fine-tuning is already running.', 'Das Nachtraining läuft bereits.', 'L’affinage est déjà en cours.',
             'El ajuste ya está en marcha.', 'L’affinamento è già in corso.', 'A afinação já está a decorrer.'),
    'stopping': ('Останавливаю дообучение; продолжу с того же места в следующий раз.', 'Stopping fine-tuning; it will continue from the same point next time.',
                 'Nachtraining wird angehalten; es geht beim nächsten Mal an derselben Stelle weiter.', 'Arrêt de l’affinage ; il reprendra au même endroit.',
                 'Detengo el ajuste; continuará desde el mismo punto la próxima vez.', 'Fermo l’affinamento; riprenderà dallo stesso punto.',
                 'A parar a afinação; continua do mesmo ponto da próxima vez.'),
    'idle': ('Дообучение сейчас не идёт.', 'Fine-tuning is not running.', 'Das Nachtraining läuft nicht.', 'L’affinage n’est pas en cours.',
             'El ajuste no está en marcha.', 'L’affinamento non è in corso.', 'A afinação não está a decorrer.'),
    'rolled_back': ('Модель ассистента: вернулся к «{model}».', 'Assistant model: back to “{model}”.', 'Assistenzmodell: zurück zu „{model}“.',
                    'Modèle d’assistant : retour à « {model} ».', 'Modelo del asistente: vuelta a «{model}».', 'Modello assistente: tornato a «{model}».',
                    'Modelo do assistente: de volta a «{model}».'),
    'check_failed': ('Не удалось проверить модель ассистента: Ollama не ответил. Это не значит, что модель не установлена.',
                     'Could not check the assistant model: Ollama did not answer. This does not mean the model is missing.',
                     'Das Assistenzmodell konnte nicht geprüft werden: Ollama hat nicht geantwortet. Das heißt nicht, dass es fehlt.',
                     'Impossible de vérifier le modèle d’assistant : Ollama n’a pas répondu. Cela ne veut pas dire qu’il manque.',
                     'No se pudo comprobar el modelo del asistente: Ollama no respondió. Eso no significa que falte.',
                     'Impossibile controllare il modello assistente: Ollama non ha risposto. Non significa che manchi.',
                     'Não foi possível verificar o modelo do assistente: o Ollama não respondeu. Isso não significa que falte.'),
    'last_failed': ('Последнее дообучение завершилось ошибкой на этапе «{stage}».', 'The last fine-tuning run failed at stage “{stage}”.',
                    'Das letzte Nachtraining ist im Schritt „{stage}“ fehlgeschlagen.', 'Le dernier affinage a échoué à l’étape « {stage} ».',
                    'El último ajuste falló en la fase «{stage}».', 'L’ultimo affinamento non è riuscito nella fase «{stage}».',
                    'A última afinação falhou na etapa «{stage}».'),
    'last_stopped': ('Последнее дообучение остановлено; продолжу с того же места.', 'The last fine-tuning run was stopped; it continues from the same point.',
                     'Das letzte Nachtraining wurde angehalten; es geht an derselben Stelle weiter.', 'Le dernier affinage a été arrêté ; il reprendra au même endroit.',
                     'El último ajuste se detuvo; continuará desde el mismo punto.', 'L’ultimo affinamento è stato fermato; riprenderà dallo stesso punto.',
                     'A última afinação foi parada; continua do mesmo ponto.'),
    'activation_failed': ('Новая версия оказалась лучше, но включить её не удалось — работает прежняя.',
                          'The new version was better, but it could not be switched on — the previous one is still in use.',
                          'Die neue Version war besser, konnte aber nicht aktiviert werden – die bisherige bleibt aktiv.',
                          'La nouvelle version était meilleure mais n’a pas pu être activée — l’ancienne reste utilisée.',
                          'La nueva versión era mejor, pero no se pudo activar: sigue la anterior.',
                          'La nuova versione era migliore ma non è stato possibile attivarla: resta la precedente.',
                          'A nova versão era melhor, mas não foi possível ativá-la — continua a anterior.'),
    'install_busy': ('Установка модели ассистента уже идёт.', 'The assistant model installation is already running.',
                     'Die Installation des Assistenzmodells läuft bereits.', 'L’installation du modèle d’assistant est déjà en cours.',
                     'La instalación del modelo del asistente ya está en marcha.', 'L’installazione del modello assistente è già in corso.',
                     'A instalação do modelo do assistente já está a decorrer.'),
    'install_refused': ('Установку модели ассистента не удалось начать: {reason}', 'Could not start the assistant model installation: {reason}',
                        'Die Installation des Assistenzmodells konnte nicht starten: {reason}', 'Impossible de lancer l’installation du modèle d’assistant : {reason}',
                        'No se pudo iniciar la instalación del modelo del asistente: {reason}', 'Impossibile avviare l’installazione del modello assistente: {reason}',
                        'Não foi possível iniciar a instalação do modelo do assistente: {reason}'),
    'installing': ('Устанавливаю модель ассистента в фоне ({size}).', 'Installing the assistant model in the background ({size}).',
                   'Das Assistenzmodell wird im Hintergrund installiert ({size}).', 'Installation du modèle d’assistant en arrière-plan ({size}).',
                   'Instalando el modelo del asistente en segundo plano ({size}).', 'Installazione del modello assistente in background ({size}).',
                   'A instalar o modelo do assistente em segundo plano ({size}).'),
}


def _t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_CODES.index(code) if code in _CODES else 1].format(**values)


def _pct(value: Any) -> str:
    return f'{float(value) * 100:.0f}%' if isinstance(value, (int, float)) else '—'


def run(config: Any, args: dict[str, Any], ollama: Any = None) -> ToolExecution:
    from jarvis.neo_llm import jobs
    code = str(config.get('app.language', 'ru') or 'ru')
    action = str(args.get('action', '') or '')
    size = str(config.get('llm_agent.size', '0.5b') or '0.5b')
    if action == 'status':
        lines = []
        lookup_failed = False
        try:
            model = ollama.resolve_agent_model() if ollama is not None else ''
        except Exception:
            model, lookup_failed = '', True     # HIST-073: «could not check» is not «not installed»
        lines.append(_t('model', code, model=model, size=size) if model else _t('check_failed' if lookup_failed else 'missing', code))
        status = jobs.read_status(config)
        stage = (status.get('status') or {}).get('stage', '')
        if stage == 'waiting_setup' and not status.get('running'):
            missing = (status.get('status') or {}).get('missing') or []
            lines.append(_t('waiting_local' if 'local Ollama endpoint' in missing else 'waiting_setup', code))
        if status.get('running'):
            train = status.get('train_status') or {}
            progress = f" {train.get('step', 0)}/{train.get('total', '?')}" if stage == 'train' and train else ''
            lines.append(_t('training', code, stage=stage or '…', progress=progress) + (_t('paused', code) if status.get('paused') else ''))
        # HIST-070: a newer failed/stopped run is said as such, never as «not run yet».
        if not status.get('running') and stage == 'failed':
            lines.append(_t('last_failed', code, stage=str((status.get('status') or {}).get('stage_failed') or '…')))
        elif not status.get('running') and stage == 'stopped':
            lines.append(_t('last_stopped', code))
        report = status.get('last_report')
        if report:
            # HIST-071: «better but not activated» is not «not better».
            verdict = ('promoted' if report.get('promoted')
                       else 'activation_failed' if report.get('better') or report.get('activation_failed') else 'rejected')
            # HIST-074: a past activation is history; «the new version is in use»
            # is said only while that very model is the current selection.
            candidate_name = str(report.get('candidate_model', '') or '').removesuffix(':latest')
            current_name = str(model or '').removesuffix(':latest')
            verdict_text = _t(verdict, code)
            if verdict == 'promoted' and candidate_name and current_name and candidate_name != current_name:
                verdict_text = _t('promoted_earlier', code, candidate=candidate_name, model=current_name)
            lines.append(_t('report', code, cand=_pct(report['candidate'].get('do_exact')), cur=_pct(report['current'].get('do_exact')),
                            chat=_pct(report['candidate'].get('chat_ok')),  # «—» when no chat rows were checked
                            verdict=verdict_text))
        elif not status.get('running') and stage not in {'waiting_setup', 'failed', 'stopped'}:
            lines.append(_t('never', code))
        return ToolExecution(True, '\n'.join(lines), {'model': model, 'status': status})
    if action == 'train':
        if jobs.running():
            return ToolExecution(True, _t('busy', code), {'running': True})
        jobs.start(config, force=True)
        return ToolExecution(True, _t('started', code), {'running': True})
    if action == 'stop':
        return ToolExecution(True, _t('stopping' if jobs.stop(config) else 'idle', code), {})
    if action == 'rollback':
        model = jobs.rollback(config)
        return ToolExecution(True, _t('rolled_back', code, model=model), {'model': model})
    if action == 'install':
        from jarvis.neo_llm.setup import background_status, ensure_background
        if not ensure_background(config, force=True):
            # HIST-072: «installing» only after a worker really started.
            line, _code, alive = background_status()
            if alive:
                return ToolExecution(True, _t('install_busy', code), {'size': size, 'running': True})
            return ToolExecution(False, _t('install_refused', code, reason=str(line or '—')[:200]), {'size': size, 'started': False})
        return ToolExecution(True, _t('installing', code, size=size), {'size': size, 'started': True})
    raise ValueError(f'unknown assistant_model action: {action}')
