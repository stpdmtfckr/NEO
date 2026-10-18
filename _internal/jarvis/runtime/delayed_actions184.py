"""Explicit one-shot desktop actions. Text/model output is never replayed as code.

Only the selected locale's complete grammar can schedule an allowlisted action.
Live targets and confirmation are captured before persistence; restart cancels all
such actions. The timer loop only enqueues IDs, and the UI uses its normal worker.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import os
import re
import time

from jarvis.runtime.duration import parse_duration, format_duration
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell

_ACTIONS = ('shutdown', 'stop_video', 'close_browser')
_NAMES = {
 'ru': ('Выключение компьютера', 'Остановка видео YouTube', 'Закрытие окна браузера'),
 'en': ('Shut down computer', 'Stop YouTube video', 'Close browser window'),
 'de': ('Computer herunterfahren', 'YouTube-Video stoppen', 'Browserfenster schließen'),
 'fr': ('Éteindre l’ordinateur', 'Arrêter la vidéo YouTube', 'Fermer la fenêtre du navigateur'),
 'es': ('Apagar el ordenador', 'Detener el vídeo de YouTube', 'Cerrar la ventana del navegador'),
 'it': ('Spegnere il computer', 'Fermare il video YouTube', 'Chiudere la finestra del browser'),
 'pt': ('Desligar o computador', 'Parar o vídeo YouTube', 'Fechar a janela do navegador'),
}
_HEADS = {
 'ru': (r'(?:выключи|выключить|отключи|заверши работу) (?:компьютер|компьютера|комп|пк)', r'(?:останови|выключи|поставь на паузу) (?:текущее )?(?:видео|ролик|видео youtube|ютуб)', r'(?:закрой|закрыть|выключи) (?:браузер|окно браузера)'),
 'en': (r'(?:shut down|shutdown|turn off|power off) (?:the |my )?(?:computer|pc)', r'(?:stop|pause|turn off) (?:the |this |current )?(?:video|youtube video|youtube)', r'(?:close|quit|exit) (?:the |my |current )?(?:browser|browser window)'),
 'de': (r'(?:fahre|fahr) (?:den )?(?:computer|pc) herunter|(?:schalte|schalt) (?:den )?(?:computer|pc) aus', r'(?:stoppe|pausiere|beende) (?:das |aktuelle )?(?:video|youtube-video)', r'(?:schließe|schliesse|beende) (?:den browser|das browserfenster|browser)'),
 'fr': (r'(?:éteins|eteins|arrête|arrete) (?:l[\' ]|le )?(?:ordinateur|pc)', r'(?:arrête|arrete|stoppe|mets en pause) (?:la |cette )?(?:vidéo|video|vidéo youtube)', r'(?:ferme|quitte) (?:le navigateur|la fenêtre du navigateur|le browser)'),
 'es': (r'(?:apaga|desconecta) (?:el |la |mi )?(?:ordenador|computador|computadora|pc)', r'(?:para|detén|deten|pausa) (?:el |este )?(?:vídeo|video|vídeo de youtube|video de youtube)', r'(?:cierra|cierre) (?:el navegador|la ventana del navegador)'),
 'it': (r'(?:spegni|arresta) (?:il |mio )?(?:computer|pc)', r'(?:ferma|interrompi|metti in pausa) (?:il |questo )?(?:video|video youtube)', r'(?:chiudi|termina) (?:il browser|la finestra del browser)'),
 'pt': (r'(?:desliga|desligue|encerra) (?:o |meu )?(?:computador|pc)', r'(?:para|pare|pausa|pause|interrompe) (?:o |este )?(?:vídeo|video|vídeo youtube)', r'(?:fecha|feche|encerra) (?:o navegador|o browser|a janela do navegador)'),
}
_AFTER = {'ru': 'через', 'en': 'in|after', 'de': 'in|nach', 'fr': 'dans|après', 'es': 'en|dentro de', 'it': 'tra|fra', 'pt': 'em|daqui a'}
_BARE_STOP = {
 'ru': r'останови|остановить|выключи|выключить|поставь на паузу',
 'en': r'stop|pause|turn off|shut down',
 'de': r'stoppe|pausiere|beende',
 'fr': r'arrête|arrete|stoppe|mets en pause',
 'es': r'para|detén|deten|pausa',
 'it': r'ferma|interrompi|metti in pausa',
 'pt': r'para|pare|pausa|pause|interrompe',
}
_LIST = {
 'ru': r'(?:покажи|список)(?: все)? (?:отложенные действия|отложенных действий)',
 'en': r'(?:show|list)(?: all)? (?:delayed|scheduled) actions',
 'de': r'(?:zeige|liste)(?: alle)? (?:geplanten|verzögerten) aktionen',
 'fr': r'(?:affiche|liste)(?: les)? actions (?:différées|programmées)',
 'es': r'(?:muestra|lista)(?: las)? acciones (?:programadas|pendientes)',
 'it': r'(?:mostra|elenca)(?: le)? azioni (?:programmate|pianificate)',
 'pt': r'(?:mostra|lista)(?: as)? ações (?:agendadas|programadas)',
}
_CANCEL = {
 'ru': r'(?:отмени|удали) (?:отложенное действие|действие)',
 'en': r'(?:cancel|remove) (?:delayed action|scheduled action|action)',
 'de': r'(?:lösche|entferne) (?:die )?(?:geplante aktion|aktion)',
 'fr': r'(?:annule|supprime) (?:l\')?(?:action différée|action programmée|action)',
 'es': r'(?:cancela|elimina) (?:la )?(?:acción programada|acción)',
 'it': r'(?:annulla|rimuovi) (?:l\')?(?:azione programmata|azione)',
 'pt': r'(?:cancela|remove) (?:a )?(?:ação agendada|ação)',
}
_MESSAGES = {
 'scheduled': ('Запланировано: {label} через {duration}. Номер {id}.', 'Scheduled: {label} in {duration}. ID {id}.', 'Geplant: {label} in {duration}. ID {id}.', 'Programmé : {label} dans {duration}. Identifiant {id}.', 'Programado: {label} en {duration}. ID {id}.', 'Programmato: {label} tra {duration}. ID {id}.', 'Agendado: {label} em {duration}. ID {id}.'),
 'confirm': ('Запланировать «{label}» через {duration}? {summary}', 'Schedule “{label}” in {duration}? {summary}', '„{label}“ in {duration} planen? {summary}', 'Programmer « {label} » dans {duration} ? {summary}', '¿Programar «{label}» en {duration}? {summary}', 'Programmare «{label}» tra {duration}? {summary}', 'Agendar «{label}» em {duration}? {summary}'),
 'which_target': ('Что нужно остановить или выключить? Повтори команду с целью: компьютер, видео YouTube или окно браузера, и задержкой.', 'What should stop or turn off? Repeat the command with the target: computer, YouTube video or browser window, and the delay.', 'Was soll angehalten oder ausgeschaltet werden? Wiederhole den Befehl mit Ziel: Computer, YouTube-Video oder Browserfenster, und Verzögerung.', 'Que faut-il arrêter ou éteindre ? Répète la commande avec la cible : ordinateur, vidéo YouTube ou fenêtre du navigateur, et le délai.', '¿Qué quieres detener o apagar? Repite la orden con el destino: ordenador, vídeo de YouTube o ventana del navegador, y el plazo.', 'Che cosa vuoi fermare o spegnere? Ripeti il comando con la destinazione: computer, video YouTube o finestra del browser, e il ritardo.', 'O que queres parar ou desligar? Repete o comando com o destino: computador, vídeo YouTube ou janela do navegador, e o prazo.'),
 'invalid': ('Укажи задержку от 1 секунды до 7 дней, например: через 10 минут.', 'Use a delay from 1 second to 7 days, for example: in 10 minutes.', 'Gib eine Verzögerung von 1 Sekunde bis 7 Tagen an, zum Beispiel: in 10 Minuten.', 'Indique un délai de 1 seconde à 7 jours, par exemple : dans 10 minutes.', 'Indica un plazo de 1 segundo a 7 días, por ejemplo: en 10 minutos.', 'Indica un ritardo da 1 secondo a 7 giorni, per esempio: tra 10 minuti.', 'Indica um prazo de 1 segundo a 7 dias, por exemplo: em 10 minutos.'),
 'target': ('Не удалось однозначно определить цель. Открой нужное окно браузера; для видео поддерживается только выбранная вкладка YouTube. Ничего не запланировано.', 'I could not identify one exact target. Select the browser window; video scheduling supports only its selected YouTube tab. Nothing was scheduled.', 'Das Ziel ist nicht eindeutig. Wähle das Browserfenster; Videos werden nur im ausgewählten YouTube-Tab unterstützt. Nichts geplant.', 'La cible n’est pas identifiable. Sélectionne la fenêtre du navigateur ; seule sa vidéo YouTube sélectionnée est prise en charge. Rien n’est programmé.', 'No pude identificar el destino. Selecciona la ventana del navegador; solo se admite su pestaña de YouTube seleccionada. No se programó nada.', 'Non ho identificato la destinazione. Seleziona la finestra del browser; è supportata solo la scheda YouTube selezionata. Nulla è programmato.', 'Não identifiquei o destino. Seleciona a janela do navegador; só é suportado o separador YouTube selecionado. Nada foi agendado.'),
 'changed': ('{label}: цель или правила подтверждения изменились. Действие не выполнено.', '{label}: the target or confirmation policy changed. No action was executed.', '{label}: Ziel oder Bestätigungsregeln geändert. Keine Aktion ausgeführt.', '{label} : la cible ou les règles de confirmation ont changé. Aucune action exécutée.', '{label}: el destino o la política de confirmación cambió. No se ejecutó la acción.', '{label}: destinazione o regole di conferma cambiate. Nessuna azione eseguita.', '{label}: o destino ou as regras de confirmação mudaram. Nenhuma ação foi executada.'),
 'restart_save_failed': ('Не удалось обновить сохранённые действия. После перезапуска они всё равно отменены; обычные таймеры продолжают работать.', 'Could not update saved actions. They remain cancelled after restart; ordinary timers continue running.', 'Gespeicherte Aktionen konnten nicht aktualisiert werden. Sie bleiben nach dem Neustart abgebrochen; normale Timer laufen weiter.', 'Impossible de mettre à jour les actions enregistrées. Elles restent annulées au redémarrage ; les minuteurs habituels continuent.', 'No se pudieron actualizar las acciones guardadas. Siguen canceladas tras reiniciar; los temporizadores normales continúan.', 'Impossibile aggiornare le azioni salvate. Restano annullate dopo il riavvio; i timer normali continuano.', 'Não foi possível atualizar as ações guardadas. Continuam canceladas após reiniciar; os temporizadores normais continuam.'),
 'restart': ('{label}: отменено после перезапуска NEO. При необходимости запланируй заново.', '{label}: cancelled after NEO restarted. Schedule it again if needed.', '{label}: nach NEO-Neustart abgebrochen. Bei Bedarf erneut planen.', '{label} : annulé au redémarrage de NEO. Reprogramme si nécessaire.', '{label}: cancelado tras reiniciar NEO. Vuelve a programarlo si hace falta.', '{label}: annullato al riavvio di NEO. Riprogramma se necessario.', '{label}: cancelado após reiniciar o NEO. Agenda novamente se necessário.'),
 'late': ('{label}: пропущено, поскольку время выполнения истекло.', '{label}: skipped because its execution window expired.', '{label}: übersprungen, da das Ausführungsfenster abgelaufen ist.', '{label} : ignoré car le délai d’exécution a expiré.', '{label}: omitido porque venció el plazo de ejecución.', '{label}: saltato perché il periodo di esecuzione è scaduto.', '{label}: ignorado porque o prazo de execução expirou.'),
 'empty': ('Нет отложенных действий.', 'No delayed actions.', 'Keine geplanten Aktionen.', 'Aucune action programmée.', 'No hay acciones programadas.', 'Nessuna azione programmata.', 'Nenhuma ação agendada.'),
 'cancelled': ('Отменено: {label}.', 'Cancelled: {label}.', 'Abgebrochen: {label}.', 'Annulé : {label}.', 'Cancelado: {label}.', 'Annullato: {label}.', 'Cancelado: {label}.'),
 'choose': ('Укажи номер или точное название действия из списка.', 'Use an action ID or its exact name from the list.', 'Nenne die ID oder den genauen Namen aus der Liste.', 'Indique l’identifiant ou le nom exact dans la liste.', 'Indica el ID o el nombre exacto de la lista.', 'Indica l’ID o il nome esatto dell’elenco.', 'Indica o ID ou o nome exato da lista.'),
 'failed': ('{label}: не удалось выполнить или подтвердить результат. Проверь состояние.', '{label}: execution failed or its result could not be verified. Check its state.', '{label}: Ausführung fehlgeschlagen oder Ergebnis nicht bestätigt. Prüfe den Zustand.', '{label} : échec ou résultat non vérifiable. Vérifie son état.', '{label}: falló o no se pudo verificar el resultado. Comprueba su estado.', '{label}: esecuzione fallita o risultato non verificabile. Controlla lo stato.', '{label}: falhou ou não foi possível verificar o resultado. Verifica o estado.'),
 'saved_failed': ('Не удалось сохранить действие. Ничего не запланировано.', 'Could not save the action. Nothing was scheduled.', 'Aktion konnte nicht gespeichert werden. Nichts geplant.', 'Impossible d’enregistrer l’action. Rien n’est programmé.', 'No se pudo guardar la acción. No se programó nada.', 'Impossibile salvare l’azione. Nulla è programmato.', 'Não foi possível guardar a ação. Nada foi agendado.'),
 'remote': ('Отложенные действия пока доступны только в окне NEO, текстом или голосом.', 'Delayed actions are currently available only in the NEO desktop window, by text or voice.', 'Geplante Aktionen sind derzeit nur im NEO-Desktopfenster per Text oder Sprache verfügbar.', 'Les actions différées sont actuellement disponibles uniquement dans la fenêtre NEO, par texte ou voix.', 'Las acciones programadas solo están disponibles en la ventana de NEO, por texto o voz.', 'Le azioni programmate sono disponibili solo nella finestra NEO, via testo o voce.', 'As ações agendadas estão disponíveis apenas na janela do NEO, por texto ou voz.'),
 'sent': ('{label}: запрос отправлен системе.', '{label}: request sent to the system.', '{label}: Anfrage an das System gesendet.', '{label} : demande envoyée au système.', '{label}: solicitud enviada al sistema.', '{label}: richiesta inviata al sistema.', '{label}: pedido enviado ao sistema.'),
 'pending': ('{label}: запрос закрытия отправлен, но окно ещё открыто. Проверь запрос сохранения.', '{label}: close requested, but the window is still open. Check for a save prompt.', '{label}: Schließen angefordert, das Fenster ist noch offen. Prüfe die Speicherabfrage.', '{label} : fermeture demandée, mais la fenêtre reste ouverte. Vérifie la demande d’enregistrement.', '{label}: se solicitó el cierre, pero la ventana sigue abierta. Comprueba si pide guardar.', '{label}: chiusura richiesta, ma la finestra è ancora aperta. Controlla se chiede di salvare.', '{label}: fecho solicitado, mas a janela continua aberta. Verifica se pede para guardar.'),
 'unsupported': ('Отложенно это действие я выполнить не могу, поэтому ничего не сделано. Могу выполнить его сейчас или поставить напоминание через {duration}.', 'I can’t run this action later, so nothing was done. I can do it now or set a reminder in {duration}.', 'Diese Aktion kann ich nicht verzögert ausführen, daher wurde nichts getan. Ich kann sie jetzt ausführen oder eine Erinnerung in {duration} stellen.', 'Je ne peux pas exécuter cette action plus tard, donc rien n’a été fait. Je peux la faire maintenant ou programmer un rappel dans {duration}.', 'No puedo ejecutar esta acción más tarde, así que no hice nada. Puedo hacerla ahora o poner un recordatorio en {duration}.', 'Non posso eseguire questa azione più tardi, quindi non ho fatto nulla. Posso farla ora o impostare un promemoria tra {duration}.', 'Não consigo executar esta ação mais tarde, por isso nada foi feito. Posso fazê-la agora ou criar um lembrete em {duration}.'),
 'done': ('{label}: выполнение подтверждено.', '{label}: completion verified.', '{label}: Ausführung bestätigt.', '{label} : exécution confirmée.', '{label}: ejecución verificada.', '{label}: esecuzione verificata.', '{label}: execução verificada.'),
}


def message(key, code, **values):
    return _MESSAGES[key][LANGUAGES.index(code if code in LANGUAGES else 'en')].format(**values)


def action_name(action, code):
    return _NAMES[code][_ACTIONS.index(action)]


def _duration_request(payload, code):
    """A delayed shell fails closed unless its payload is an explicit browser.

    Unknown or incomplete quantities must never become immediate media actions.
    Browser names are the narrow non-time exception to ambiguous 'in/dans/em'.
    """
    value = norm(payload)
    browser = r'firefox|mozilla(?: firefox)?|chrome|google chrome|edge|microsoft edge|opera(?: gx)?|brave|vivaldi|safari|yandex(?: browser)?|яндекс(?: браузер)?'
    return re.fullmatch(browser, value, re.I) is None


def parse(text, code):
    if code not in LANGUAGES:
        return None
    value = norm(strip_request_shell(text, code))
    if re.fullmatch(_LIST[code], value):
        return {'operation': 'list'}
    cancel = re.fullmatch(_CANCEL[code] + r'(?:\s+(.+))?', value)
    if cancel:
        return {'operation': 'cancel', 'selector': str(cancel.group(1) or '')}
    for action, head in zip(_ACTIONS, _HEADS[code]):
        # NEO 188 atlas: the delay word must be a whole word («turn off instant replay» has no delay).
        match = re.fullmatch(r'(?:' + head + r')\s+(?:' + _AFTER[code] + r')(?:\s+|(?=\d)|$)(.*)', value)
        if match:
            seconds = parse_duration((match.group(1) or ""), code, allow_clock=False)
            if seconds is None and not _duration_request((match.group(1) or ""), code):
                return None
            return {'operation': 'schedule', 'action': action, 'seconds': seconds}
    ambiguous = re.fullmatch(r'(?:' + _BARE_STOP[code] + r')\s+(?:' + _AFTER[code] + r')(?:\s+|(?=\d)|$)(.*)', value)
    if ambiguous and _duration_request((ambiguous.group(1) or ""), code):
        return {'operation': 'clarify_target', 'seconds': parse_duration((ambiguous.group(1) or ""), code, allow_clock=False)}
    # German separable verbs place the prefix after the time.
    if code == 'de':
        match = re.fullmatch(r'(?:fahre|fahr) (?:den )?(?:computer|pc) (?:in|nach)\s*(.+) herunter', value)
        if match:
            return {'operation': 'schedule', 'action': 'shutdown', 'seconds': parse_duration(match.group(1), code, allow_clock=False)}
    return None


_LATER_VERBS188 = {
 'ru': r'открой|открыть|запусти|запустить|закрой|закрыть|включи|включить|сыграй|поставь музыку|покажи',
 'en': r'open|launch|start|run|close|play|turn on|show',
 'de': r'öffne|offne|starte|schließe|schliesse|spiele|zeige',
 'fr': r'ouvre|lance|démarre|demarre|ferme|joue|affiche',
 'es': r'abre|inicia|ejecuta|cierra|reproduce|pon|muestra',
 'it': r'apri|avvia|esegui|chiudi|riproduci|mostra',
 'pt': r'abre|abra|inicia|inicie|executa|fecha|feche|toca|reproduza|mostra',
}
# Only unambiguous «after a delay» words: es «en» / pt «em» also mean «at» («pon la posición en 90 segundos»).
_LATER_AFTER188 = {'ru': 'через', 'en': 'in|after', 'de': 'in|nach', 'fr': 'dans', 'es': 'dentro de', 'it': 'tra|fra', 'pt': 'daqui a'}
_TIMER_WORDS188 = (r'таймер|напомин|напомни|будильник|timer|remind|alarm|wecker|erinner|minuteur|rappel|réveil|'
                   r'temporizador|recordatorio|alarma|promemoria|sveglia|lembrete|alarme')


def unsupported_later188(text, code):
    """Seconds of a «do X later» request this module cannot schedule, else None.

    NEO 188 atlas (proposed.timer.schedule_action): «открой блокнот через 5 минут» is
    a request for LATER. Without a delayed executor it must not run now with the
    delay glued to the application name.
    """
    if code not in LANGUAGES:
        return None
    value = norm(strip_request_shell(text, code))
    after = _LATER_AFTER188[code]
    polite = r'(?:(?:bitte|please|пожалуйста|por favor|per favore|s\'il te pla[iî]t)[,\s]+)?'
    later = (re.fullmatch(r'(?P<head>(?:' + _LATER_VERBS188[code] + r')\s+.+?)\s+(?:' + after + r')\s+(?P<delay>.+)', value)
             or re.fullmatch(r'(?:' + after + r')\s+(?P<delay>.+?)[,\s]+' + polite + r'(?P<head>(?:' + _LATER_VERBS188[code] + r')\s+.+)', value))
    if not later or re.search(_TIMER_WORDS188, later.group('head'), re.I):
        return None
    return parse_duration(later.group('delay'), code, allow_clock=False)


def tool_call(action):
    return {'shutdown': ('system', {'action': 'shutdown', 'delay_seconds': 0}),
            'stop_video': ('media', {'action': 'stop', 'target': 'youtube'}),
            'close_browser': ('window', {'action': 'close', 'title': 'browser'})}[action]


def validate_spec(spec):
    if not isinstance(spec, dict) or spec.get('action') not in _ACTIONS:
        raise ValueError('Unknown delayed action')
    if spec.get('language') not in LANGUAGES or not isinstance(spec.get('target'), dict):
        raise ValueError('Invalid action binding')
    if not isinstance(spec.get('risk'), int) or not isinstance(spec.get('confirmed'), bool):
        raise ValueError('Missing action authorization')
    if set(spec) != {'action', 'language', 'target', 'risk', 'confirmed'}:
        raise ValueError('Unexpected action fields')


_BOUND = ContextVar('neo_bound_delayed_action184', default=None)

@contextmanager
def bound_target(spec):
    token = _BOUND.set(spec)
    try:
        yield
    finally:
        _BOUND.reset(token)


def current_binding(action):
    spec = _BOUND.get()
    return spec['target'] if spec and spec['action'] == action else None


def _window_identity(window):
    import psutil
    pid = int(window.element_info.process_id)
    process = psutil.Process(pid)
    return {'hwnd': int(window.handle), 'pid': pid, 'created': process.create_time(),
            'process': process.name().casefold()}


def _foreground_identity():
    import ctypes
    from ctypes import wintypes
    if os.name != 'nt':
        raise ValueError('Windows target binding is unavailable')
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.GetForegroundWindow.restype = wintypes.HWND
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    hwnd = api.GetForegroundWindow()
    pid = wintypes.DWORD()
    api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(hwnd or 0), int(pid.value)


def capture_target(action):
    if action == 'shutdown':
        return {}
    from jarvis import browser_control as browser
    tab = browser.selected_browser_tab()
    if tab is None:
        raise ValueError('Select a browser window')
    # NEO may own focus while the user types, but a foreground third-party
    # player must never be silently reinterpreted as an old YouTube tab.
    foreground, foreground_pid = _foreground_identity()
    if foreground != int(tab.window.handle):
        if foreground_pid != os.getpid():
            raise ValueError('Select the requested browser first')
        if browser._LAST_BROWSER_HANDLE != int(tab.window.handle) and len(browser._browser_windows()) != 1:
            raise ValueError('Several browser windows; select one first')
    identity = _window_identity(tab.window)
    if action == 'close_browser':
        return {**identity, 'label': tab.window_title}
    browser._tab_domain(tab, 'youtube')
    # No URL means the exact video cannot be bound safely.
    if not tab.url or not browser._url_matches_service(tab.url, 'youtube'):
        raise ValueError('Select an identifiable YouTube video')
    info = browser._run_winrt_coro(browser._read_exact_tab_media_session_async(tab, 'video', strict_match=True), timeout=4.)
    if not info.get('source') or not info.get('title'):
        raise ValueError('Missing media identity')
    return {**identity, 'url': tab.url, 'tab_title': tab.title,
            'media': {key: str(info.get(key, '') or '') for key in ('source', 'title', 'artist', 'album')},
            'label': str(info['title'])}


def resolve_target(target):
    from jarvis import browser_control as browser
    rows = browser._browser_windows(target.get('process', ''))
    for window in rows:
        if int(window.handle) == target.get('hwnd') and _window_identity(window) == {key: target[key] for key in ('hwnd', 'pid', 'created', 'process')}:
            return window
    raise ValueError('The original browser window is gone')


def bound_video_tab(target):
    from jarvis import browser_control as browser
    window = resolve_target(target)
    selected, index = browser._selected_tab_with_index(window)
    title = str(selected.window_text() or selected.element_info.name or '') if selected else ''
    url = browser._strict_selected_tab_url(window, timeout=.75)
    if not selected or title != target['tab_title'] or url != target['url']:
        raise ValueError('The selected video changed')
    return browser.BrowserTab(title, target['process'], str(window.window_text() or ''), selected, window, index=index, url=url)


def execute_bound_video(target):
    from jarvis import browser_control as browser
    tab = bound_video_tab(target)
    # The expected media identity is rechecked on the SAME session used for pause.
    return browser._run_winrt_coro(browser._control_exact_tab_media_session_async(
        tab, 'pause', 'video', expected_session=target['media']), timeout=7.)


def execute_bound_close(target):
    import ctypes
    from ctypes import wintypes
    resolve_target(target)
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    api.IsWindow.argtypes = [wintypes.HWND]
    # Graceful close only, bound to one HWND and process creation identity.
    if not api.PostMessageW(target['hwnd'], 0x0010, 0, 0):
        raise OSError('Close request was rejected')
    deadline = time.monotonic() + 1.5
    while api.IsWindow(target['hwnd']) and time.monotonic() < deadline:
        time.sleep(.05)
    verified = not bool(api.IsWindow(target['hwnd']))
    return {'verified': verified, 'requested': True, 'pending': not verified}


def _manager(agent, code):
    from jarvis.runtime.timers import timer_manager
    return timer_manager(agent.config.paths.root, code)


def handle(agent, text, code):
    from jarvis.agent import AgentTurn, PendingAction
    from jarvis.routing.ownership_firewall import bind_exact_tool_ownership, current_turn_claim
    request = parse(text, code)
    if request is None:
        later188 = unsupported_later188(text, code)
        if later188 is None:
            return None
        return AgentTurn(message('unsupported', code, duration=format_duration(later188, code)),
                         success=False, outcome='clarification', tool_details=['delayed_actions184.unsupported188'])
    if getattr(agent, '_conversation_key', 'desktop') != 'desktop':
        return AgentTurn(message('remote', code), success=False, outcome='clarification')
    if request['operation'] in {'schedule', 'clarify_target'}:
        agent._pending_context = None
        conversations = getattr(agent, 'conversations', None)
        if conversations is not None:
            conversations.get(getattr(agent, '_conversation_key', 'desktop')).pending_intent = None
    if request['operation'] == 'clarify_target':
        return AgentTurn(message('which_target' if request['seconds'] is not None else 'invalid', code), success=False, outcome='clarification')
    manager = _manager(agent, code)
    rows = [row for row in manager.list() if row.kind == 'action']
    if request['operation'] == 'list':
        reply = '\n'.join(f'{row.id}: {row.label} — {format_duration(manager.remaining(row), code)}' for row in rows)
        return AgentTurn(reply or message('empty', code), success=True)
    if request['operation'] == 'cancel':
        selector = norm(request['selector'])
        matches = [row for row in rows if selector in {row.id, norm(row.label), norm(action_name(row.action_spec['action'], code))}]
        if not selector and len(rows) == 1:
            matches = rows
        if len(matches) != 1:
            return AgentTurn(message('choose' if rows else 'empty', code), success=False, outcome='clarification')
        try:
            removed = manager.cancel(matches[0].id)
        except OSError:
            return AgentTurn(message('failed', code, label=matches[0].label), success=False)
        return AgentTurn(message('cancelled', code, label=matches[0].label) if removed else message('empty', code), success=bool(removed))
    seconds = request['seconds']
    if seconds is None:
        return AgentTurn(message('invalid', code), success=False, outcome='clarification')
    action = request['action']
    label = action_name(action, code)
    origin = _origin188()
    try:
        target = capture_target(action)
    except Exception:
        return AgentTurn(message('target', code), success=False, outcome='clarification')
    if _origin_cancelled188(origin):
        # HIST-031: «стоп» arrived while the target was being captured.
        return AgentTurn(message('cancelled', code, label=label), success=False, outcome='cancelled')
    if target.get('label'):
        label += ' · ' + str(target['label'])[:120]
    tool, args = tool_call(action)
    try:
        risk = agent.tools.risk_for(tool, args)
    except Exception:
        return AgentTurn(message('failed', code, label=label), success=False)
    bind_exact_tool_ownership(tool, text=text)
    spec = {'action': action, 'target': target, 'language': code, 'risk': int(risk), 'confirmed': False}
    schedule = {'seconds': seconds, 'label': label, 'spec': spec}
    if agent._needs_confirmation(tool, args, risk):
        summary = message('confirm', code, label=label, duration=format_duration(seconds, code), summary=agent._confirmation_summary(tool, args, risk))
        agent._pending_context = {'created_at': time.monotonic(), 'delayed_schedule184': schedule,
                                  'messages': [{'role': 'user', 'content': text}],
                                  'call': {'function': {'name': tool, 'arguments': args}},
                                  'tool_names': [tool], 'ownership_claim': current_turn_claim()}
        return AgentTurn(summary, pending=PendingAction(tool, args, risk, summary))
    return save(agent, schedule, origin=origin)


def _origin188() -> int:
    from jarvis.runtime.cancellation import cancellation_manager
    return cancellation_manager().cancel_count()


def _origin_cancelled188(origin: int) -> bool:
    """True when the request that began at ``origin`` was cancelled since then.

    An older cancellation (a previous «стоп») never blocks a new request; only a
    cancel issued after this request began, covering its generation, does."""
    from jarvis.runtime.cancellation import cancellation_manager
    try:
        return bool(cancellation_manager().cancelled_since(origin))
    except Exception:
        return False


def save(agent, schedule, *, origin: int | None = None):
    from jarvis.agent import AgentTurn
    spec = schedule['spec']; code = spec['language']
    origin = _origin188() if origin is None else origin
    try:
        # Revalidate after any confirmation delay.
        if spec['action'] == 'stop_video':
            bound_video_tab(spec['target'])
        elif spec['action'] == 'close_browser':
            resolve_target(spec['target'])
        if _origin_cancelled188(origin):
            # HIST-031: a cancel during the revalidation closes the admission too.
            return AgentTurn(message('cancelled', code, label=schedule['label']), success=False, outcome='cancelled')
        item = _manager(agent, code).create_action(schedule['seconds'], schedule['label'], spec)
    except (OSError, ValueError, RuntimeError):
        return AgentTurn(message('saved_failed', code), success=False)
    return AgentTurn(message('scheduled', code, label=item.label, duration=format_duration(schedule['seconds'], code), id=item.id), success=True, outcome='scheduled')


def confirm(agent, context, approved):
    from jarvis.agent import AgentTurn
    schedule = deepcopy(context['delayed_schedule184'])
    if not approved:
        return AgentTurn(message('cancelled', schedule['spec']['language'], label=schedule['label']), outcome='cancelled')
    schedule['spec']['confirmed'] = True
    return save(agent, schedule)


def run_due(agent, timer_id):
    from jarvis.agent import AgentTurn
    from jarvis.language import language_code
    from jarvis.routing.ownership_firewall import bind_exact_tool_ownership, ownership_claim_scope
    from jarvis.tools.registry import suppress_execution_learning184
    code = language_code(agent.config)
    from jarvis.runtime.timers import ActionExpiredError
    manager = _manager(agent, code)
    try:
        item = manager.take_action(timer_id)
    except ActionExpiredError as exc:
        return AgentTurn(message('late', code, label=str(exc)), success=False, outcome='cancelled')
    except OSError:
        return AgentTurn(message('failed', code, label=timer_id), success=False)
    if item is None:
        return AgentTurn(message('cancelled', code, label=timer_id), success=False, outcome='cancelled')
    spec = item.action_spec; code = spec['language']
    try:
        validate_spec(spec)
        tool, args = tool_call(spec['action'])
        risk = agent.tools.risk_for(tool, args)
        if int(risk) > spec['risk'] or (agent._needs_confirmation(tool, args, risk) and not spec['confirmed']):
            return AgentTurn(message('changed', code, label=item.label), success=False)
        try:
            if spec['action'] == 'stop_video':
                bound_video_tab(spec['target'])
            elif spec['action'] == 'close_browser':
                resolve_target(spec['target'])
        except (ValueError, OSError, RuntimeError):
            return AgentTurn(message('changed', code, label=item.label), success=False)
        with ownership_claim_scope(None), bound_target(spec), suppress_execution_learning184():
            bind_exact_tool_ownership(tool, text=item.label)
            result = agent.tools.execute(tool, args)
        data = result.data if isinstance(result.data, dict) else {}
        status = 'pending' if spec['action'] == 'close_browser' and data.get('requested') and data.get('pending') else 'sent' if spec['action'] == 'shutdown' and result.success else 'done' if result.success and data.get('verified') is True else 'failed'
        return AgentTurn(message(status, code, label=item.label), success=status in {'sent', 'done'}, tool_details=['delayed_action.' + spec['action']], outcome=result.outcome)
    except Exception:
        return AgentTurn(message('failed', code, label=item.label), success=False)
