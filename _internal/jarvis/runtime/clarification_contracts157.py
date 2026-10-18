"""Typed missing values for native action heads, never guessed arguments.

All entries describe real registered tool fields. A completed request returns to
the normal schema, ownership and confirmation boundary in Agent.
"""
from __future__ import annotations

from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell
import re

TYPED_EXAMPLES157 = {
 'timer.reschedule': (
  ('измени таймер','перенеси срабатывание таймера'),('reschedule timer','change the countdown of a timer'),('verschiebe den timer','ändere die ablaufzeit des timers'),('reprogramme le minuteur','change le délai du minuteur'),('reprograma el temporizador','cambia la cuenta atrás del temporizador'),('riprogramma il timer','cambia il conto alla rovescia del timer'),('reprograma o temporizador','altera a contagem decrescente do temporizador')),
 'desktop.close_folder': (
  ('закрой папку','закрой открытое окно проводника'),('close a folder','close an open Explorer folder window'),('schließe einen Ordner','schließe ein offenes Explorer-Ordnerfenster'),('ferme un dossier','ferme une fenêtre de dossier de l’Explorateur'),('cierra una carpeta','cierra una ventana de carpeta del Explorador'),('chiudi una cartella','chiudi una finestra di cartella di Esplora file'),('fecha uma pasta','fecha uma janela de pasta do Explorador')),
}


def slot(name, kind='name', prompt=None):
    return (name, kind, prompt or 'field157:' + name)


CONTRACTS = {
    'automation.queue': (slot('calls','calls157','field157:calls_queue157'),),
    'automation.parallel': (slot('calls','calls157','field157:calls_parallel157'),),
    'timer.reschedule': (slot('id',prompt='timer_id156'),slot('seconds','duration','duration')),
    'timer.timer': (slot('seconds','duration','duration'),),
    'clipboard.set': (slot('text','text','clipboard_text'),),
    'application.kill': (slot('name', prompt='application'),),
    'audio.change_volume': (slot('value', 'number157:-100:100', 'field157:volume_delta'),),
    'audio.change_target_volume': (slot('target', prompt='application'), slot('value', 'number157:-100:100', 'field157:volume_delta')),
    'audio.set_input_mute': (slot('muted', 'bool157'),),
    'audio.duck_system': (slot('value', 'percent156', 'percent156'),),
    'autoclicker.configure': (slot('cps', 'number157:0.5:100'), slot('button', 'mouse_button157')),
    'autoclicker.set_rate': (slot('cps', 'number157:0.5:100'),),
    'autoclicker.set_button': (slot('button', 'mouse_button157'),),
    'browser_advanced.activate_by_topic': (slot('query'),),
    'clipboard.copy_history': (slot('offset','int157:1:100','field157:index'),),
    'clipboard.paste_history': (slot('offset','int157:1:100','field157:index'),),
    'browser_advanced.group_by_topic': (slot('query'),),
    'desktop.resolve': (slot('query'),),
    'developer.generate_python_app': (slot('description', 'text'), slot('project_name', 'filename'), slot('output_dir', 'path', 'destination')),
    'developer.run_python_file': (slot('file_path', 'absolute_source', 'file_path156'),),
    'discord.detach_text_channel': (slot('channel', prompt='channel156'),),
    'email.search': (slot('query'),),
    'email.read': (slot('message_id'),),
    'email.send': (slot('to', 'email', 'email_to'), slot('subject', 'text', 'email_subject'), slot('body', 'text', 'email_body')),
    'email.reply': (slot('message_id'), slot('body', 'text', 'email_body')),
    'filesystem.open': (slot('source', 'path', 'file_path156'),),
    'filesystem.delete': (slot('source', 'absolute_source', 'source'),),
    'filesystem.delete_permanent': (slot('source', 'absolute_source', 'source'),),
    'filesystem.replace_text': (slot('source', 'absolute_source', 'file_path156'), slot('old_text', 'text'), slot('content', 'text')),
    'filesystem.create_file': (slot('folder', 'path', 'destination'), slot('name', 'filename', 'new_name')),
    'filesystem.insert_text': (slot('source', 'absolute_source', 'file_path156'), slot('line', 'int157:1:1000000'), slot('content', 'text')),
    'google_calendar.add': (slot('title'), slot('date', 'date157'), slot('time', 'clock', 'alarm_time'), slot('duration_minutes', 'int157:5:1440')),
    'google_calendar.update': (slot('event_id'), slot('title'), slot('date', 'date157'), slot('time', 'clock', 'alarm_time')),
    'google_calendar.delete': (slot('event_id'),),
    'input_control.type_text': (slot('text', 'text'),),
    'input_control.hotkey': (slot('keys', 'hotkey157'),),
    'input_control.click': (slot('x', 'int157:-32768:32767'), slot('y', 'int157:-32768:32767')),
    'input_control.click_control': (slot('name'),),
    'input_control.focus_control': (slot('name'),),
    'input_control.set_control_text': (slot('name'), slot('text', 'text')),
    'media.play_search': (slot('query'),),
    'media.seek_forward': (slot('seconds', 'duration'),),
    'media.seek_backward': (slot('seconds', 'duration'),),
    'media.seek_to': (slot('position_seconds', 'number157:0:86400'),),
    'memory.remember': (slot('key'), slot('value', 'text')),
    'memory.forget': (slot('key'),),
    'music.start_track': (slot('query', prompt='query'),),
    'music.playlist': (slot('playlist',prompt='playlist'),),
    'spotify.playlist': (slot('playlist',prompt='playlist'),),
    'yandex_music.playlist': (slot('playlist',prompt='playlist'),),
    'music.play_track': (slot('query', prompt='query'),),
    'spotify.play_track': (slot('query', prompt='query'),),
    'yandex_music.play_track': (slot('query', prompt='query'),),
    'music.set_volume': (slot('volume', 'percent156', 'percent156'),),
    'music.seek': (slot('seconds', 'number157:0:86400'),),
    'spotify.start_track': (slot('query', prompt='query'),),
    'spotify.set_volume': (slot('volume', 'percent156', 'percent156'),),
    'spotify.seek': (slot('seconds', 'number157:0:86400'),),
    'yandex_music.start_track': (slot('query', prompt='query'),),
    'yandex_music.set_volume': (slot('volume', 'percent156', 'percent156'),),
    'network.ping': (slot('host', 'host157'),),
    'scenario.run': (slot('name'),),
    'search.content': (slot('query', 'text'),),
    'screen.click_visual': (slot('instruction', 'text'),),
    'system.terminate_process': (slot('process_name', prompt='application'),),
    'system.set_bluetooth': (slot('enabled', 'bool157'),),
    'system.set_wifi': (slot('enabled', 'bool157'),),
    'system.set_brightness': (slot('value', 'percent156', 'field157:brightness'),),
    'system.set_power_plan': (slot('power_plan'),),
    'system.set_mouse_sensitivity': (slot('value', 'int157:1:20', 'field157:sensitivity'),),
    'system.change_mouse_sensitivity': (slot('value', 'int157:-19:19', 'field157:sensitivity_delta'),),
    'telegram_messages.from_sender': (slot('sender'),),
    'telegram_messages.send': (slot('target'), slot('text', 'text')),
    'text_edit.dictate': (slot('text', 'text'),),
    'text_edit.replace_last_sentence': (slot('text', 'text'),),
    'transfer.page_url_to_discord': (slot('channel', prompt='channel156'),),
    'transfer.last_screenshot_to_discord': (slot('channel', prompt='channel156'),),
    'transfer.attach_file_discord': (slot('path', 'absolute_source', 'file_path156'), slot('channel', prompt='channel156')),
    'window.move_resize': (slot('title', prompt='window156'), slot('x', 'int157:-32768:32767'), slot('y', 'int157:-32768:32767'), slot('width', 'int157:100:32767'), slot('height', 'int157:100:32767')),
    'window.move_to_monitor': (slot('title', prompt='window156'), slot('monitor', 'int157:1:16')),
    'window_advanced.list_instances': (slot('query', prompt='application'),),
    'window_advanced.focus_instance': (slot('query', prompt='application'), slot('index', 'int157:1:50')),
    'window_advanced.move_instance': (slot('query', prompt='application'), slot('index','int157:1:50'),slot('monitor','int157:1:16')),
    'window_advanced.close_with_content': (slot('content', 'text'),),
    'window_advanced.keep_only_content': (slot('content', 'text'),),
}

for action in ('show', 'hide', 'close', 'delete', 'clear_text', 'checklist_clear', 'checklist_complete_all', 'checklist_uncheck_all'):
    CONTRACTS['overlay.' + action] = (slot('name', prompt='overlay_name156'),)
for action in ('set_text', 'append_text', 'remove_text'):
    CONTRACTS['overlay.' + action] = (slot('name', prompt='overlay_name156'), slot('text', 'text'))
CONTRACTS['overlay.checklist_add'] = (slot('name', prompt='panel_name'), slot('item', 'text'))
CONTRACTS['overlay.checklist_remove'] = (slot('name', prompt='panel_name'), slot('item', 'positive_integer', 'item_index'))
CONTRACTS['overlay.style'] = (slot('name', prompt='overlay_name156'), slot('font_size', 'int157:8:96'))

# Labels describe actual fields, in the selected language, rather than telling
# every text recipient to write a note or every object recipient to name a file.
FIELDS = {
 'calls_queue157': ('от двух до восьми команд чтения или открытия; раздели их словом «затем» или точкой с запятой','two to eight read or open commands, separated by then or semicolons','zwei bis acht Lese- oder Öffnungsbefehle, getrennt durch dann oder Semikolons','deux à huit commandes de lecture ou d’ouverture, séparées par puis ou des points-virgules','entre dos y ocho órdenes de lectura o apertura, separadas por luego o punto y coma','da due a otto comandi di lettura o apertura, separati da poi o punti e virgola','duas a oito ordens de leitura ou abertura, separadas por depois ou ponto e vírgula'),
 'calls_parallel157': ('от двух до восьми независимых команд чтения; раздели их точкой с запятой','two to eight independent read-only commands, separated by semicolons','zwei bis acht unabhängige Lesebefehle, getrennt durch Semikolons','deux à huit commandes de lecture indépendantes, séparées par des points-virgules','entre dos y ocho órdenes de lectura independientes, separadas por punto y coma','da due a otto comandi di lettura indipendenti, separati da punti e virgola','duas a oito ordens de leitura independentes, separadas por ponto e vírgula'),
 'calls': ('две или больше команд чтения, разделённые точкой с запятой; например: покажи память; покажи процессор','two or more read-only commands separated by semicolons, such as: show memory; show cpu','mindestens zwei Lesebefehle, getrennt durch Semikolons','au moins deux commandes de lecture, séparées par des points-virgules','dos o más órdenes de lectura, separadas por punto y coma','almeno due comandi di lettura, separati da punti e virgola','dois ou mais comandos de leitura, separados por ponto e vírgula'),
 'voice': ('имя установленного голоса','installed voice name','Namen einer installierten Stimme','nom d’une voix installée','nombre de una voz instalada','nome di una voce installata','nome de uma voz instalada'),
 'target_language': ('язык перевода','target language','Zielsprache','langue cible','idioma de destino','lingua di destinazione','idioma de destino'),
 'script': ('код PowerShell следующей репликой','PowerShell code in the next message','PowerShell-Code in der nächsten Nachricht','code PowerShell dans le prochain message','código PowerShell en el siguiente mensaje','codice PowerShell nel prossimo messaggio','código PowerShell na próxima mensagem'),
 'query': ('запрос или название объекта','search text or object name','Suchtext oder Objektname','recherche ou nom de l’objet','búsqueda o nombre del objeto','ricerca o nome dell’oggetto','pesquisa ou nome do objeto'),
 'name': ('точное имя объекта','exact object name','genauen Objektnamen','nom exact de l’objet','nombre exacto del objeto','nome esatto dell’oggetto','nome exato do objeto'),
 'title': ('название события','event title','Ereignistitel','titre de l’événement','título del evento','titolo dell’evento','título do evento'),
 'text': ('текст следующей репликой','text in your next message','Text in deiner nächsten Nachricht','texte dans le prochain message','texto en tu siguiente mensaje','testo nel prossimo messaggio','texto na próxima mensagem'),
 'content': ('текст следующей репликой','text in your next message','Text in deiner nächsten Nachricht','texte dans le prochain message','texto en tu siguiente mensaje','testo nel prossimo messaggio','texto na próxima mensagem'),
 'old_text': ('точный текст для замены','exact text to replace','genauen zu ersetzenden Text','texte exact à remplacer','texto exacto que se reemplazará','testo esatto da sostituire','texto exato a substituir'),
 'message_id': ('идентификатор письма из списка','message ID from the list','Nachrichten-ID aus der Liste','identifiant du message dans la liste','identificador del mensaje de la lista','ID del messaggio nell’elenco','identificador da mensagem na lista'),
 'event_id': ('идентификатор события из списка','event ID from the list','Ereignis-ID aus der Liste','identifiant de l’événement dans la liste','identificador del evento de la lista','ID dell’evento nell’elenco','identificador do evento na lista'),
 'date': ('дату в формате ГГГГ-ММ-ДД','date as YYYY-MM-DD','Datum als JJJJ-MM-TT','date au format AAAA-MM-JJ','fecha como AAAA-MM-DD','data come AAAA-MM-GG','data como AAAA-MM-DD'),
 'duration_minutes': ('длительность в минутах (5–1440)','duration in minutes (5–1440)','Dauer in Minuten (5–1440)','durée en minutes (5–1440)','duración en minutos (5–1440)','durata in minuti (5–1440)','duração em minutos (5–1440)'),
 'cps': ('число кликов в секунду (0,5–100)','clicks per second (0.5–100)','Klicks pro Sekunde (0,5–100)','clics par seconde (0,5–100)','clics por segundo (0,5–100)','clic al secondo (0,5–100)','cliques por segundo (0,5–100)'),
 'button': ('кнопку мыши: левая, правая или средняя','mouse button: left, right or middle','Maustaste: links, rechts oder Mitte','bouton : gauche, droit ou milieu','botón: izquierdo, derecho o central','pulsante: sinistro, destro o centrale','botão: esquerdo, direito ou central'),
 'muted': ('выключить или включить звук микрофона','whether to mute or unmute the microphone','ob das Mikrofon stumm oder hörbar sein soll','s’il faut couper ou activer le micro','si silenciar o activar el micrófono','se silenziare o attivare il microfono','se silenciar ou ativar o microfone'),
 'enabled': ('включить или выключить','on or off','ein oder aus','activer ou désactiver','activar o desactivar','attivare o disattivare','ativar ou desativar'),
 'volume_delta': ('изменение громкости от −100 до 100, со знаком','volume change from −100 to 100, with its sign','Lautstärkeänderung von −100 bis 100 mit Vorzeichen','variation du volume de −100 à 100, avec signe','cambio de volumen de −100 a 100, con signo','variazione del volume da −100 a 100, con segno','alteração do volume de −100 a 100, com sinal'),
 'brightness': ('яркость от 0 до 100','brightness from 0 to 100','Helligkeit von 0 bis 100','luminosité de 0 à 100','brillo de 0 a 100','luminosità da 0 a 100','brilho de 0 a 100'),
 'sensitivity': ('чувствительность мыши от 1 до 20','mouse sensitivity from 1 to 20','Mausempfindlichkeit von 1 bis 20','sensibilité de la souris de 1 à 20','sensibilidad del ratón de 1 a 20','sensibilità del mouse da 1 a 20','sensibilidade do rato de 1 a 20'),
 'sensitivity_delta': ('изменение чувствительности от −19 до 19','sensitivity change from −19 to 19','Empfindlichkeitsänderung von −19 bis 19','variation de sensibilité de −19 à 19','cambio de sensibilidad de −19 a 19','variazione di sensibilità da −19 a 19','alteração de sensibilidade de −19 a 19'),
 'keys': ('сочетание клавиш, например Ctrl+Shift+S','key combination, such as Ctrl+Shift+S','Tastenkombination, etwa Ctrl+Shift+S','raccourci clavier, par exemple Ctrl+Shift+S','combinación de teclas, como Ctrl+Shift+S','combinazione di tasti, come Ctrl+Shift+S','combinação de teclas, como Ctrl+Shift+S'),
 'host': ('имя хоста или IP-адрес','hostname or IP address','Hostnamen oder IP-Adresse','nom d’hôte ou adresse IP','nombre del host o dirección IP','nome host o indirizzo IP','nome do servidor ou endereço IP'),
 'key': ('название записи памяти','memory entry name','Namen des Speichereintrags','nom de l’entrée mémoire','nombre del recuerdo','nome della voce in memoria','nome da entrada de memória'),
 'value': ('значение следующей репликой','value in your next message','Wert in deiner nächsten Nachricht','valeur dans le prochain message','valor en tu siguiente mensaje','valore nel prossimo messaggio','valor na próxima mensagem'),
 'sender': ('имя отправителя','sender name','Absendernamen','nom de l’expéditeur','nombre del remitente','nome del mittente','nome do remetente'),
 'target': ('точное имя получателя','exact recipient name','genauen Empfängernamen','nom exact du destinataire','nombre exacto del destinatario','nome esatto del destinatario','nome exato do destinatário'),
 'power_plan': ('имя плана питания из списка','power plan name from the list','Energiesparplannamen aus der Liste','nom du mode d’alimentation dans la liste','nombre del plan de energía de la lista','nome del piano energetico nell’elenco','nome do plano de energia na lista'),
 'description': ('описание программы','program description','Programmbeschreibung','description du programme','descripción del programa','descrizione del programma','descrição do programa'),
 'project_name': ('имя проекта','project name','Projektnamen','nom du projet','nombre del proyecto','nome del progetto','nome do projeto'),
 'instruction': ('что именно найти на экране','what to find on the screen','was auf dem Bildschirm gesucht werden soll','ce qu’il faut trouver à l’écran','qué buscar en la pantalla','cosa cercare sullo schermo','o que procurar no ecrã'),
 'line': ('номер строки','line number','Zeilennummer','numéro de ligne','número de línea','numero di riga','número da linha'),
 'monitor': ('номер монитора от 1 до 16','monitor number from 1 to 16','Monitornummer von 1 bis 16','numéro d’écran de 1 à 16','número de monitor de 1 a 16','numero del monitor da 1 a 16','número do monitor de 1 a 16'),
 'index': ('номер из списка','number from the list','Nummer aus der Liste','numéro dans la liste','número de la lista','numero nell’elenco','número da lista'),
 'font_size': ('размер шрифта от 8 до 96','font size from 8 to 96','Schriftgröße von 8 bis 96','taille de police de 8 à 96','tamaño de fuente de 8 a 96','dimensione del carattere da 8 a 96','tamanho da letra de 8 a 96'),
 'seconds': ('позицию в секундах от 0 до 86400','position in seconds from 0 to 86400','Position in Sekunden von 0 bis 86400','position en secondes de 0 à 86400','posición en segundos de 0 a 86400','posizione in secondi da 0 a 86400','posição em segundos de 0 a 86400'),
}
FIELDS['position_seconds'] = FIELDS['seconds']
FIELDS['item'] = FIELDS['text']
for key, ru, en, de, fr, es, it, pt in (
 ('x','координату X','X coordinate','X-Koordinate','coordonnée X','coordenada X','coordinata X','coordenada X'),
 ('y','координату Y','Y coordinate','Y-Koordinate','coordonnée Y','coordenada Y','coordinata Y','coordenada Y'),
 ('width','ширину окна в пикселях (не меньше 100)','window width in pixels (at least 100)','Fensterbreite in Pixeln (mindestens 100)','largeur en pixels (au moins 100)','ancho en píxeles (al menos 100)','larghezza in pixel (almeno 100)','largura em píxeis (pelo menos 100)'),
 ('height','высоту окна в пикселях (не меньше 100)','window height in pixels (at least 100)','Fensterhöhe in Pixeln (mindestens 100)','hauteur en pixels (au moins 100)','alto en píxeles (al menos 100)','altezza in pixel (almeno 100)','altura em píxeis (pelo menos 100)'),
): FIELDS[key] = (ru,en,de,fr,es,it,pt)


def question(key, code):
    i = LANGUAGES.index(code)
    label = FIELDS[key.split(':',1)[1]][i]
    return ('Укажи {value}.','Please provide {value}.','Bitte nenne {value}.','Indique {value}.','Indica {value}.','Indica {value}.','Indica {value}.')[i].format(value=label)


def normalize_head(text, code):
    """Only used for exact known heads; never applied to a captured payload."""
    value = norm(strip_request_shell(text, code))
    words = ('сейчас','now','jetzt','maintenant','ahora','adesso|ora','agora')[LANGUAGES.index(code)]
    return re.sub(r'\s+', ' ', re.sub(r'(?<!\w)(?:' + words + r')(?!\w)', ' ', value)).strip()


def extra_partial(text, code):
    value=normalize_head(text,code);i=LANGUAGES.index(code)
    if value in {normalize_head(p,code) for p in TYPED_EXAMPLES157['timer.reschedule'][i]}:
        return 'timer',{'action':'reschedule'},CONTRACTS['timer.reschedule']
    if code=='en' and value=='set the clipboard':
        return 'clipboard',{'action':'set'},CONTRACTS['clipboard.set']
    from jarvis.runtime.script_requests157 import HEADS as SCRIPT_HEADS
    if value in {normalize_head(p,code) for p in SCRIPT_HEADS[i]}:
        return 'powershell',{},(slot('script','text'),)
    extras=(
      (('переведи','translate','übersetze','traduis','traduce','traduci','traduz'),'translate',{},(slot('text','text'),slot('target_language'))),
      (('выполни скрипт powershell','run a powershell script','führe ein powershell skript aus','exécute un script powershell','ejecuta un script powershell','esegui uno script powershell','executa um script powershell'),'powershell',{},(slot('script','text'),)),
      (('измени таймер','reschedule timer','verschiebe den timer','reprogramme le minuteur','reprograma el temporizador','riprogramma il timer','reprograma o temporizador'),'timer',{'action':'reschedule'},CONTRACTS['timer.reschedule']),
    )
    for phrases,tool,args,slots in extras:
        if value==normalize_head(phrases[i],code):
            return tool,args,slots
    return None


def parse_calls(value,code,parse_complete, *, action='queue'):
    from jarvis.runtime.automation_requests157 import parse_calls as parse_plan
    return parse_plan(value,code,parse_complete,action=action)
