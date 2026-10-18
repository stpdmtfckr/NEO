"""Reviewed missing-argument contracts over the shipped native grammar.

Only exact literal command heads are accepted, with at least two words and an
unambiguous action. This is NOT general prefix/fuzzy execution. Slot completion
returns to Agent's schema, ownership, cancellation and risk checks.
"""
from __future__ import annotations
from functools import lru_cache
import re
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell
from jarvis.runtime.clarification_contracts157 import CONTRACTS as CONTRACTS157, normalize_head

# name, validation kind, localized question. No unrestricted JSON/object slots.
CONTRACTS = {
 'overlay.rename': (('name','name','overlay_name156'),('new_name','name','overlay_new_name156')),
 'overlay.move': (('name','name','overlay_name156'),('position','overlay_position156','overlay_position156')),
 'application.find': (('name','name','application'),),
 'application.info': (('name','name','application'),),
 'browser.search': (('value','name','web_query156'),),
 'browser.search_first': (('value','name','web_query156'),),
 'browser.open_url': (('value','url','url'),),
 'browser.activate_tab_title': (('value','name','tab_title'),),
 'browser.activate_tab': (('index','bounded_index','tab_index156'),),
 'filesystem.list': (('source','path','folder_path156'),),
 'filesystem.read_text': (('source','absolute_source','file_path156'),),
 'filesystem.info': (('source','absolute_source','file_path156'),),
 'filesystem.exists': (('source','absolute_source','file_path156'),),
 'filesystem.hash': (('source','absolute_source','file_path156'),),
 'filesystem.reveal': (('source','absolute_source','file_path156'),),
 'filesystem.tree': (('source','path','folder_path156'),),
 'filesystem.disk_usage': (('source','path','folder_path156'),),
 'filesystem.write_text': (('source','absolute_source','file_path156'),('content','text','text')),
 'filesystem.append_text': (('source','absolute_source','file_path156'),('content','text','text')),
 'filesystem.zip': (('source','absolute_source','source'),('destination','absolute_source','destination')),
 'filesystem.unzip': (('source','absolute_source','source'),('destination','path','destination')),
 'filesystem.search_text': (('query','text','text'),('source','absolute_source','file_path156')),
 'search.files': (('query','name','file'),),
 'window.focus': (('title','name','window156'),),
 'window.minimize': (('title','name','window156'),),
 'window.maximize': (('title','name','window156'),),
 'window.restore': (('title','name','window156'),),
 'window.close': (('title','name','window156'),),
 'audio.set_volume': (('value','percent156','percent156'),),
 'audio.set_input_volume': (('value','percent156','percent156'),),
 'audio.set_default': (('device_name','name','audio_device156'),),
 'audio.route_app_device': (('target','name','application'),('device_name','name','audio_device156')),
 'audio.mute_target': (('target','name','application'),),
 'audio.mute_except': (('target','name','application'),),
 'shelf.read': (('title','name','title'),),
 'shelf.get': (('title','name','title'),),
 'shelf.copy': (('title','name','title'),),
 'shelf.pin': (('title','name','title'),),
 'shelf.delete': (('title','name','title'),),
 'shelf.update': (('title','name','title'),('content','text','text')),
 'shelf.import_file': (('path','absolute_source','file_path156'),),
 'timer.reminder': (('seconds','duration','reminder_after156'),('label','text','reminder156')),
 'timer.cancel': (('id','name','timer_id156'),),
 'discord.send_message': (('channel','name','channel156'),('text','text','text')),
 'discord.list_channels': (('server','name','server156'),),
 'discord.bot_join_voice': (('channel','name','channel156'),),
 'discord.connect_channel': (('channel','name','channel156'),),
 'discord.attach_text_channel': (('channel','name','channel156'),),
}
CONTRACTS.update(CONTRACTS157)

# Native shorter forms keep action and object; they do not infer an action from a noun.
ALIASES = {
 'automation.queue': ('задачи по очереди','tasks in order','Aufgaben nacheinander','tâches dans l’ordre','tareas en orden','attività in sequenza','tarefas por ordem'),
 'automation.parallel': ('задачи параллельно','tasks in parallel','Aufgaben parallel','tâches en parallèle','tareas en paralelo','attività in parallelo','tarefas em paralelo'),
 'search.files': ('найди файл','find a file','finde eine Datei','trouve un fichier','busca un archivo','trova un file','encontra um ficheiro'),
 'overlay.rename': ('переименовать панель','rename panel','benenne das Panel','renommer le panneau','renombra el panel','rinominare il pannello','renomeia o painel'),
 'overlay.move': ('переместить панель','move the panel','verschiebe das Panel','déplacer le panneau','mover el panel','spostare il pannello','move o painel'),
 'shelf.copy': ('скопируй заметку','copy the note','kopiere die Notiz','copie la note','copia la nota','copia la nota','copia a nota'),
 'browser.search': ('найди в интернете','search the web','suche im internet','cherche sur internet','busca en internet','cerca su internet','procura na internet'),
 'audio.set_volume': ('установи громкость','set the volume','stelle die lautstärke ein','règle le volume','ajusta el volumen','imposta il volume','define o volume'),
 'audio.set_input_volume': ('установи громкость микрофона','set microphone volume','stelle die mikrofonlautstärke ein','règle le volume du microphone','ajusta el volumen del micrófono','imposta il volume del microfono','define o volume do microfone'),
 'timer.reminder': ('создай напоминание','create a reminder','erstelle eine erinnerung','crée un rappel','crea un recordatorio','crea un promemoria','cria um lembrete'),
 'discord.send_message': ('отправь сообщение в дискорд','send a discord message','sende eine discord nachricht','envoie un message discord','envía un mensaje de discord','invia un messaggio discord','envia uma mensagem no discord'),
}

@lru_cache(maxsize=7)
def index(code: str) -> dict[str, tuple[str, dict, tuple]]:
    from jarvis.runtime.canonical_commands import templates
    from pathlib import Path
    import json
    from jarvis.tools.argument_validation import normalize_tool_call
    def canonical(intent):
        tool, action = intent.split('.', 1)
        if action == 'play_music':
            return intent  # existing default play/My Wave semantics need no query
        tool, args = normalize_tool_call(tool, {'action': action})
        return 'search.files' if (tool, args['action']) == ('filesystem','find') else tool + '.' + args['action']
    owners: dict[str, set[str]] = {}
    for row in templates(code):
        match = re.search(r'\{[a-z_]+\}', row.phrase)
        if match is None:
            owners.setdefault(normalize_head(row.phrase, code), set()).add(canonical(row.intent) if row.intent in CONTRACTS157 else '__complete__')
            continue
        head = normalize_head(row.phrase[:match.start()], code)
        if len(head.split()) >= 2:
            owners.setdefault(head, set()).add(canonical(row.intent))
    pack = json.loads((Path(__file__).resolve().parents[1] / 'locales/commands' / (code + '.json')).read_text(encoding='utf-8'))
    exact_owners = {}
    for intent, phrases in pack.get('action_phrases', {}).items():
        if intent in {'discord.list_channels','filesystem.disk_usage'}:
            continue  # optional server/drive: preserve the integration's safe default
        for phrase in phrases:
            if '{' not in phrase and '}' not in phrase:
                head=normalize_head(phrase, code)
                owners.setdefault(head, set()).add(canonical(intent))
                exact_owners.setdefault(head,set()).add(canonical(intent))
    i = LANGUAGES.index(code)
    for intent, phrases in ALIASES.items():
        owners.setdefault(normalize_head(phrases[i], code), set()).add(intent)
    result = {}
    for head, intents in owners.items():
        if len(exact_owners.get(head,set())) == 1:
            # A unique declared whole request outranks a truncated longer
            # template. "delete file" means recycle; the omitted suffix
            # "permanently" cannot make that request destructive or ambiguous.
            intents = exact_owners[head]
        if len(intents) != 1:
            continue  # similar templates can disagree about source/action
        intent = next(iter(intents))
        # file search uses the existing dedicated search tool.
        intent = 'search.files' if intent == 'filesystem.find' else intent
        slots = CONTRACTS.get(intent)
        if slots is None:
            continue
        tool, action = intent.split('.', 1)
        args = {'action': action}
        if intent == 'audio.set_default':
            args['device_type'] = 'output'
        if intent == 'shelf.pin':
            args['pinned'] = True
        result[head] = (tool, args, slots)
    return result


def partial(text: str, code: str):
    if code not in LANGUAGES:
        return None
    # These short nouns have an unambiguous task but are missing one value.
    from jarvis.runtime.clarification_contracts157 import slot, extra_partial
    extra = extra_partial(text,code)
    if extra:
        return extra
    i = LANGUAGES.index(code)
    value = norm(strip_request_shell(text, code))
    for phrases, tool, args, slots in (
        (('громкость','volume','lautstärke','volume','volumen','volume','volume'), 'audio', {'action':'set_volume'}, (slot('value','percent156','percent156'),)),
        (('будильник','alarm','wecker','réveil','alarma','sveglia','alarme'), 'timer', {'action':'alarm'}, (slot('text','clock','alarm_time'),)),
        (('напомни','remind me','erinnere mich','rappelle-moi','recuérdame','ricordami','lembra-me'), 'timer', {'action':'reminder'}, (slot('seconds','duration','reminder_after156'),slot('label','text','reminder156'))),
    ):
        if value == norm(phrases[i]):
            return tool, args, slots
    found = index(code).get(normalize_head(text, code))
    if found and found[1].get('action') == 'set_input_mute' and re.match(_MIC_VERB178, value):
        return None     # «выключи микрофон»: the verb already says on or off — the router runs it at once
    if found and _filled_by_declared_phrase188(found, text, code):
        return None     # NEO 188 atlas: «включи Bluetooth» is a whole declared request, not a question
    return found


def _filled_by_declared_phrase188(found, text: str, code: str) -> bool:
    """A declared whole phrase whose fixed polarity fills every missing slot.

    «включи Bluetooth» / «turn on Wi-Fi» are declared complete for the on
    direction; asking «on or off?» about them was a contradiction.
    """
    from jarvis.runtime.canonical_commands import _FIXED
    tool, args, slots = found
    intent = f"{tool}.{args.get('action', '')}"
    fixed = _FIXED.get(intent) or {}
    names = [item[0] if isinstance(item, tuple) else getattr(item, 'name', '') for item in (slots or ())]
    if not names or not all(name in fixed for name in names):
        return False
    return normalize_head(text, code) in _declared_whole_heads188(code).get(intent, frozenset())


@lru_cache(maxsize=7)
def _declared_whole_heads188(code: str) -> dict[str, frozenset[str]]:
    from jarvis.runtime.canonical_commands import templates
    from pathlib import Path
    import json
    heads: dict[str, set[str]] = {}
    for row in templates(code):
        if '{' not in row.phrase:
            heads.setdefault(row.intent, set()).add(normalize_head(row.phrase, code))
    pack = json.loads((Path(__file__).resolve().parents[1] / 'locales/commands' / (code + '.json')).read_text(encoding='utf-8'))
    for intent, phrases in pack.get('action_phrases', {}).items():
        for phrase in phrases:
            if '{' not in phrase:
                heads.setdefault(intent, set()).add(normalize_head(phrase, code))
    return {intent: frozenset(values) for intent, values in heads.items()}


# NEO 178: on/off verbs that already fill the «muted» value of a microphone request.
_MIC_VERB178 = (r'(?:выключи|отключи|заглуши|выруби|включи|верни|mute|unmute|turn\s+(?:on|off)|switch\s+(?:on|off)|schalte|stelle|'
                r'coupe|active|d[eé]sactive|r[eé]active|silencia|activa|desactiva|silenzia|attiva|disattiva|riattiva|silencia|ativa|desativa|liga|desliga)\b')


def folder_close_request(text: str, code: str) -> bool:
    from jarvis.runtime.dialogue_tasks import _CLOSE, _FOLDER
    from jarvis.runtime.natural_commands import VERBS
    i = LANGUAGES.index(code)
    from jarvis.runtime.clarification_contracts157 import TYPED_EXAMPLES157
    if normalize_head(text,code) in {normalize_head(p,code) for p in TYPED_EXAMPLES157['desktop.close_folder'][i]}:
        return True
    if code == 'ru' and norm(strip_request_shell(text, code)) == 'закрою папку':
        return True
    value = strip_request_shell(text, code).strip(' .!?')
    verbs = r'(?:' + _CLOSE[i] + '|' + VERBS[code][1] + ')'
    return bool(re.fullmatch(verbs + r'\s+' + _FOLDER[i], value, re.I) or
                (code == 'de' and re.fullmatch(_FOLDER[i] + r'\s+' + verbs, value, re.I)))


@lru_cache(maxsize=7)
def _head_owners(code: str):
    from jarvis.runtime.canonical_commands import templates
    owners = {}
    for row in templates(code):
        match = re.search(r'\{[a-z_]+\}', row.phrase)
        if match:
            head = norm(strip_request_shell(row.phrase[:match.start()], code))
            if len(head.split()) >= 2:
                owners.setdefault(head, set()).add(row.intent.split('.', 1)[0])
    return owners


def foreign_head(text: str, code: str, tool: str) -> bool:
    owners = _head_owners(code).get(norm(strip_request_shell(text, code)), set())
    return bool(owners and tool not in owners)


_POSITION_KEYS156 = ('top_left','top_center','top_right','center_left','center','center_right','bottom_left','bottom_center','bottom_right')
_POSITION_LABELS156 = {
 'ru': ('слева вверху','сверху по центру','справа вверху','слева по центру','по центру','справа по центру','слева внизу','снизу по центру','справа внизу'),
 'en': ('top left','top center','top right','center left','center','center right','bottom left','bottom center','bottom right'),
 'de': ('oben links','oben mittig','oben rechts','mittig links','in der mitte','mittig rechts','unten links','unten mittig','unten rechts'),
 'fr': ('en haut à gauche','en haut au centre','en haut à droite','au milieu à gauche','au centre','au milieu à droite','en bas à gauche','en bas au centre','en bas à droite'),
 'es': ('arriba a la izquierda','arriba en el centro','arriba a la derecha','en el centro a la izquierda','en el centro','en el centro a la derecha','abajo a la izquierda','abajo en el centro','abajo a la derecha'),
 'it': ('in alto a sinistra','in alto al centro','in alto a destra','al centro a sinistra','al centro','al centro a destra','in basso a sinistra','in basso al centro','in basso a destra'),
 'pt': ('no alto à esquerda','no alto ao centro','no alto à direita','ao centro à esquerda','ao centro','ao centro à direita','em baixo à esquerda','em baixo ao centro','em baixo à direita'),
}


def overlay_position156(value: str, code: str) -> str | None:
    """An exact finite placement vocabulary. No coordinates or fuzzy directions."""
    raw = norm(value)
    table = {norm(label):key for key,label in zip(_POSITION_KEYS156,_POSITION_LABELS156[code])}
    table.update({key:key for key in _POSITION_KEYS156})
    # Native corner formulations already used by the complete phrase grammar.
    corner = {
      'ru': {'в правый верхний угол':'top_right','в левый верхний угол':'top_left','в правый нижний угол':'bottom_right','в левый нижний угол':'bottom_left','в центр':'center'},
      'en': {'to the top right corner':'top_right','to the top left corner':'top_left','to the bottom right corner':'bottom_right','to the bottom left corner':'bottom_left','in the center':'center'},
      'de': {'in die obere rechte ecke':'top_right','in die obere linke ecke':'top_left','in die untere rechte ecke':'bottom_right','in die untere linke ecke':'bottom_left'},
      'fr': {'dans le coin supérieur droit':'top_right','dans le coin supérieur gauche':'top_left','dans le coin inférieur droit':'bottom_right','dans le coin inférieur gauche':'bottom_left'},
      'es': {'a la esquina superior derecha':'top_right','a la esquina superior izquierda':'top_left','a la esquina inferior derecha':'bottom_right','a la esquina inferior izquierda':'bottom_left'},
      'it': {'nell’angolo in alto a destra':'top_right','nell’angolo in alto a sinistra':'top_left','nell’angolo in basso a destra':'bottom_right','nell’angolo in basso a sinistra':'bottom_left'},
      'pt': {'para o canto superior direito':'top_right','para o canto superior esquerdo':'top_left','para o canto inferior direito':'bottom_right','para o canto inferior esquerdo':'bottom_left'},
    }
    table.update({norm(label):key for label,key in corner[code].items()})
    return table.get(raw)
