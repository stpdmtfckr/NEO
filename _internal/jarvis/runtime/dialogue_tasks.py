"""Typed, bounded clarification/correction state for the deterministic command path.

This module proposes calls; it NEVER executes one. Completed proposals re-enter
Agent's ordinary risk/ownership/confirmation boundary. No string concatenation
of an untrusted answer with a command and no cross-conversation state are used.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import lru_cache
import json
import ntpath
from pathlib import Path
import re
import time
from typing import Any, Callable

from jarvis.runtime.dialogue_messages import message
from jarvis.runtime.duration import parse_duration, parse_timer_command, timer_payload
from jarvis.runtime.natural_commands import LANGUAGES, looks_like_new_command, negated_action, norm, strip_request_shell
from jarvis.runtime.spoken_numbers import parse_spoken_number
from jarvis.runtime.utterance_purpose import classify_utterance

Proposal = tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class Slot:
    name: str
    kind: str
    prompt: str
    hint: str = ''
    choices: tuple[tuple[str, str], ...] = ()


@dataclass
class PendingIntent:
    tool: str
    arguments: dict[str, Any]
    slots: tuple[Slot, ...]
    language: str
    expires_at: float
    created_at: float = field(default_factory=time.monotonic)
    purpose: str = 'clarification'
    guard157: dict = field(default_factory=dict)

    def missing(self) -> Slot | None:
        return next((slot for slot in self.slots if slot.name not in self.arguments), None)

    def question(self) -> str:
        slot = self.missing()
        if slot is None:
            return ''
        if slot.kind == 'confirm':
            return message('confirm_correction', self.language, source=slot.hint)
        if slot.kind == 'choice' and slot.choices and slot.prompt != 'which_timer188':
            choices = '\n'.join(f'{i}. {label}' for i, (label, _) in enumerate(slot.choices, 1))
            if slot.prompt == 'folder_choose156':
                return message(slot.prompt, self.language, count=len(slot.choices), choices=choices)
            return message('choose', self.language, choices=choices)
        if slot.kind.startswith('scenario188:'):
            return slot.hint              # NEO 188 NEO-SCENARIO-001: the question for exactly this run input
        if slot.kind == 'unit188':
            from jarvis.runtime.param_repair188 import unit_question
            return unit_question(float(self.arguments.get('_amount188') or 0), slot.hint, self.language)
        if slot.kind == 'choice' and slot.prompt == 'which_timer188':
            from jarvis.runtime.param_repair188 import text as param_text
            return param_text('which_timer', self.language, choices='\n'.join(f'{i}. {label}' for i, (label, _) in enumerate(slot.choices, 1)))
        if slot.kind == 'absolute_source' and slot.hint:
            return message('source_location', self.language, name=slot.hint)
        prompt = slot.prompt
        if prompt == 'source' and self.tool == 'filesystem' and str(self.arguments.get('action', '')) in {'copy', 'move', 'rename'}:
            # PHRASE185-CLARIFY-COPY-01: ask for the file of THIS operation («скопировать»), not «изменить».
            prompt = 'source_' + str(self.arguments['action'])
        if prompt.startswith('peripheral160:'):
            from jarvis.runtime.short_peripheral160 import question
            return question(prompt, self.language)
        if prompt in {'game160', 'location160'}:
            from jarvis.runtime.short_requests160 import GAME_QUESTIONS, LOCATION_QUESTIONS
            return (GAME_QUESTIONS if prompt == 'game160' else LOCATION_QUESTIONS)[LANGUAGES.index(self.language)]
        if prompt.startswith('field157:'):
            from jarvis.runtime.clarification_contracts157 import question
            return question(prompt, self.language)
        if prompt == 'target_for_kind':
            prompt = self.arguments.get('kind', 'file')
        return message(prompt, self.language)

    def snapshot(self) -> dict[str, Any]:
        result = asdict(self)
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 65536:
            raise ValueError('Dialogue context exceeds its size budget')
        return result


@dataclass(frozen=True)
class DialogueDecision:
    text: str
    reply: str | None = None
    feature: Proposal | None = None
    purpose: str = 'unknown'
    from_context: bool = False
    cancel_confirmation: bool = False
    guard157: dict = field(default_factory=dict)


_OPEN = ('открой|открыть|покажи', 'open|show', 'öffne|öffnen|zeige', 'ouvre|ouvrir|montre', 'abre|abrir|muestra', 'apri|aprire|mostra', '(?:abre|abra|abrir|mostra)(?: agora)?')
_APP = (r'(?:приложение|программу)', r'(?:(?:the|an?|my)\s+)?(?:application|app|program)', r'(?:(?:die|das|eine|ein)\s+)?(?:anwendung|app|programm)', r"(?:(?:une|la)\s+|l['’])?application", r'(?:(?:la|una)\s+)?(?:aplicación|app|programa)', r"(?:(?:una|la)\s+|l['’])?(?:applicazione|app|programma)", r'(?:(?:a|o|uma|um)\s+)?(?:aplicação|aplicativo|programa)')
_FILE = (r'файл', r'(?:(?:the|a|my)\s+)?file', r'(?:(?:die|eine)\s+)?datei', r'(?:(?:un|le)\s+)?fichier', r'(?:(?:el|un)\s+)?archivo', r'(?:(?:il|un)\s+)?file', r'(?:(?:o|um)\s+)?(?:ficheiro|arquivo)')
_FOLDER = (r'папку', r'(?:(?:the|a|my)\s+)?(?:folder|directory)', r'(?:(?:den|einen)\s+)?ordner', r'(?:(?:un|le)\s+)?dossier', r'(?:(?:la|una)\s+)?carpeta', r'(?:(?:la|una)\s+)?cartella', r'(?:(?:a|uma)\s+)?pasta')
_MUTATE = {
 'copy': ('скопируй|копируй', 'copy', 'kopiere', 'copie', 'copia', 'copia', 'copia|copie'),
 'move': ('перемести|перенеси', 'move', 'verschiebe', 'déplace', 'mueve|traslada', 'sposta', 'move|mova|desloca'),
 'rename': ('переименуй', 'rename', 'benenne', 'renomme', 'renombra', 'rinomina', 'renomeia|renomeie'),
}
_CLOSE = ('закрой', 'close', 'schließe', 'ferme', 'cierra', 'chiudi', 'fecha|feche')
_NOTE = (r'создай заметку', r'create (?:a )?note', r'erstelle (?:eine )?notiz', r'crée (?:une )?note', r'crea (?:una )?nota', r'crea (?:una )?nota', r'cria (?:uma )?nota')
_SONG = (r'(?:включи|поставь) (?:песню|трек)', r'play (?:a |the )?(?:song|track)', r'spiele (?:ein |das )?lied', r'(?:joue|mets|lance) (?:une |la )?chanson', r'(?:pon|reproduce) (?:una |la )?canción', r'(?:metti|riproduci) (?:una |la )?canzone', r'(?:põe|toca|reproduz) (?:uma |a )?música')
_KIND_WORDS = {
 'ru': {'файл':'file','папку':'folder','папка':'folder','приложение':'application','программу':'application'},
 'en': {'file':'file','folder':'folder','directory':'folder','application':'application','app':'application','program':'application'},
 'de': {'datei':'file','ordner':'folder','anwendung':'application','app':'application','programm':'application'},
 'fr': {'fichier':'file','dossier':'folder','application':'application'},
 'es': {'archivo':'file','carpeta':'folder','aplicación':'application','programa':'application'},
 'it': {'file':'file','cartella':'folder','applicazione':'application','programma':'application'},
 'pt': {'ficheiro':'file','arquivo':'file','pasta':'folder','aplicação':'application','aplicativo':'application'},
}
_YES = {'ru':{'да','подтверждаю','верно'},'en':{'yes','confirm','correct'},'de':{'ja','bestätigen','richtig'},'fr':{'oui','confirmer','exact'},'es':{'sí','si','confirmar','correcto'},'it':{'sì','si','confermo','esatto'},'pt':{'sim','confirmar','certo'}}
_SYSTEM_FOLDERS = {'документы','рабочий стол','загрузки','изображения','музыка','видео', 'documents','desktop','downloads','pictures','music','videos','dokumente','bilder','musik','bureau','téléchargements','images','musique','vidéos','documentos','escritorio','descargas','imágenes','música','vídeos','documenti','scrivania','scaricamenti','immagini','musica','ambiente de trabalho','transferências','imagens'}
_MUTATING = {'copy', 'move', 'rename', 'delete', 'delete_permanent', 'write_text', 'append_text', 'replace_text', 'insert_text'}


@lru_cache(maxsize=7)
def _heads(code: str):
    i = LANGUAGES.index(code)
    rows = []
    from jarvis.runtime.natural_commands import VERBS
    adverbs = (r'сейчас|пожалуйста|мне', r'now|please', r'bitte|jetzt', r'maintenant', r'ahora', r'adesso|ora', r'agora')
    for action, verbs in (('open', _OPEN), ('close', _CLOSE)):
        native_verbs = VERBS[code][0 if action == 'open' else 1]
        prefix = r'^(?:' + verbs[i] + '|' + native_verbs + r')(?:\s+(?:' + adverbs[i] + r'))?\s+'
        rows.append(('application', action, 'name', 'name', 'application', re.compile(prefix + _APP[i] + r'(?:\s+(?P<tail>.*))?$', re.I)))
    for noun, kind in ((_FILE, 'file'), (_FOLDER, 'folder')):
        rows.append(('desktop', 'open', 'query', 'name', kind, re.compile(r'^(?:' + _OPEN[i] + r')\s+' + noun[i] + r'(?:\s+(?P<tail>.*))?$', re.I)))
    for action, verbs in _MUTATE.items():
        rows.append(('filesystem', action, 'source', 'absolute_source', 'source', re.compile(r'^(?:' + verbs[i] + r')(?:\s+' + _FILE[i] + r')?(?:\s+(?P<tail>.*))?$', re.I)))
    return tuple(rows)


def _absolute(value: str) -> bool:
    return ntpath.isabs(value) or Path(value).is_absolute()


def _quoted(value: str) -> bool:
    return len(value) >= 2 and (value[0], value[-1]) in {('"','"'), ('«','»'), ('“','”')}


def _clean(value: str, slot: Slot, code: str) -> str:
    value = value.strip()
    if _quoted(value):
        return value[1:-1]
    if slot.kind in {'path', 'absolute_source'}:
        prefixes = {'ru': r'(?:в|из|в папку|из папки|папка|папку|файл)', 'en': r'(?:to|into|in|from|the folder|folder|file)', 'de': r'(?:nach|in|im|aus|ordner|datei)', 'fr': r'(?:vers|dans|depuis|le dossier|dossier|fichier)', 'es': r'(?:a|en|desde|la carpeta|carpeta|archivo)', 'it': r'(?:in|nella|dalla|la cartella|cartella|file)', 'pt': r'(?:para|na|da|a pasta|pasta|ficheiro|arquivo)'}
        value = re.sub(r'^(?:' + prefixes[code] + r')\s+', '', value, count=1, flags=re.I)
    return value.strip()


def _validate(slot: Slot, answer: str, code: str) -> Any:
    raw = str(answer).strip()
    quoted = _quoted(raw)
    value = _clean(raw, slot, code)
    if not value or len(value) > (8000 if slot.kind == 'text' else 1000) or '\x00' in value:
        return None
    if slot.kind.startswith(('number157:', 'int157:')):
        kind, lower, upper = slot.kind.split(':')
        from math import isfinite
        try:
            number = float(value.replace(',', '.'))
        except ValueError:
            number = parse_spoken_number(value, code)
        if number is None or not isfinite(number) or not float(lower) <= number <= float(upper):
            return None
        if kind == 'int157':
            return int(number) if number == int(number) else None
        return number
    if slot.kind == 'date157':
        from datetime import date
        try:
            return date.fromisoformat(value).isoformat() if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) else None
        except ValueError:
            return None
    if slot.kind == 'bool157':
        yes = ('включить|включи|да','on|enable|yes','ein|einschalten|ja','activer|oui','activar|sí|si','attivare|sì|si','ativar|sim')
        no = ('выключить|выключи|нет','off|disable|no','aus|ausschalten|nein','désactiver|non','desactivar|no','disattivare|no','desativar|não')
        i = LANGUAGES.index(code)
        # muted=True means silence; enabled=True means active.
        positive = bool(re.fullmatch(yes[i], norm(value)))
        negative = bool(re.fullmatch(no[i], norm(value)))
        if not (positive or negative):
            return None
        return (not positive) if slot.name == 'muted' else positive
    if slot.kind == 'mouse_button157':
        labels = (('левая','правая','средняя'),('left','right','middle'),('links','rechts','mitte'),('gauche','droit','milieu'),('izquierdo','derecho','central'),('sinistro','destro','centrale'),('esquerdo','direito','central'))
        table = dict(zip(labels[LANGUAGES.index(code)],('left','right','middle')))
        return table.get(norm(value))
    if slot.kind == 'hotkey157':
        keys = [part.strip().lower() for part in value.split('+')]
        allowed = {'ctrl','alt','shift','win','enter','tab','escape','esc','space','delete','backspace','home','end','up','down','left','right','pageup','pagedown'} | set('abcdefghijklmnopqrstuvwxyz0123456789') | {'f'+str(i) for i in range(1,25)}
        return keys if 1 <= len(keys) <= 5 and len(set(keys)) == len(keys) and all(key in allowed for key in keys) else None
    if slot.kind == 'host157':
        return value if not value.startswith('-') and len(value) <= 253 and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]*',value) else None
    if slot.kind == 'overlay_position156':
        from jarvis.runtime.clarification_contracts156 import overlay_position156
        return overlay_position156(value, code)
    if slot.kind == 'zoom160':
        raw = re.sub(r'\s*(?:%|процент(?:а|ов)?|percent|prozent|pour cent|por ciento|per cento|por cento)$', '', value, flags=re.I)
        try: number=float(raw.replace(',', '.'))
        except ValueError: number=parse_spoken_number(raw,code)
        return number if number is not None and 25 <= number <= 500 else None
    if slot.kind == 'duration':
        return parse_duration(value, code)
    if slot.kind == 'unit188':
        return None  # answered by _unit_answer188; anything else re-asks the same question
    if slot.kind.startswith('scenario188:'):
        from jarvis.runtime.scenario_inputs188 import check_value, parse_value
        _prefix, kind, upper = (slot.kind.split(':') + ['30'])[:3]
        spec = {'type': kind, 'choices': [chosen for _label, chosen in slot.choices]}
        parsed = parse_value(spec, value, code)
        if parsed is None or check_value(spec, parsed, [float(upper)] if kind == 'duration' else []):
            return None
        return parsed
    if slot.kind == 'confirm':
        from jarvis.runtime.confirm_words187 import is_yes
        return True if norm(value) in _YES[code] or is_yes(raw, code) else None
    if slot.kind == 'open_kind':
        return _KIND_WORDS[code].get(norm(value)) or {'1':'file','2':'folder','3':'application'}.get(norm(value))
    if slot.kind == 'choice':
        number = int(value) if value.isdigit() else parse_spoken_number(value, code)
        if number is not None and number == int(number) and 1 <= number <= len(slot.choices):
            return slot.choices[int(number)-1][1]
        choices = [chosen for label, chosen in slot.choices if norm(value) in {norm(label), norm(chosen)}]
        return choices[0] if len(choices) == 1 else None
    if slot.kind == 'email':
        return value if re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+',value) else None
    if slot.kind == 'url':
        from urllib.parse import urlsplit
        try:
            parsed=urlsplit(value)
            return value if parsed.scheme in {'http','https'} and parsed.hostname and not parsed.username and not parsed.password else None
        except ValueError:
            return None
    if slot.kind in {'percent156', 'bounded_index'}:
        value = re.sub(r'\s*(?:%|процент(?:а|ов)?|percent(?:s)?|prozent|pour cent|por ciento|per cento|por cento)$', '', value, flags=re.I).strip()
        number = int(value) if value.isdigit() else parse_spoken_number(value, code)
        low, high = (0, 100) if slot.kind == 'percent156' else (1, 100)
        return int(number) if number is not None and number == int(number) and low <= number <= high else None
    if slot.kind == 'positive_integer':
        number = int(value) if value.isdigit() else parse_spoken_number(value, code)
        return str(int(number)) if number is not None and number == int(number) and 1 <= number <= 10000 else None
    if slot.kind == 'clock':
        # Explicit local HH:MM; a specific question is safer than guessing a date.
        match=re.fullmatch(r'(?:в\s+|at\s+)?(\d{1,2})[:.](\d{2})',value)
        return f'{int(match[1]):02d}:{int(match[2]):02d}' if match and int(match[1])<24 and int(match[2])<60 else None
    if slot.kind == 'text':
        return value
    if '\n' in value or '\r' in value:
        return None
    if not quoted and norm(value) in _YES[code] | {'?', '...', 'не знаю', 'i do not know', "i don't know"}:
        return None
    if slot.kind == 'name' and not quoted and norm(value) in _KIND_WORDS.get(code, {}):
        return None
    if slot.kind == 'filename':
        return value if value not in {'.','..'} and not re.search(r'[<>:"/\\|?*]', value) else None
    if slot.kind == 'absolute_source':
        if not _absolute(value):
            return None
        if slot.hint:
            if value.endswith(('\\', '/')) or Path(value).is_dir():
                return ntpath.join(value, slot.hint) if '\\' in value else str(Path(value) / slot.hint)
            if ntpath.basename(value).casefold() != slot.hint.casefold():
                return None  # a folder path is not silently used as a file
        return value
    if slot.kind == 'path':
        return value if _absolute(value) or norm(value) in _SYSTEM_FOLDERS else None
    return value


def _new_pending(tool: str, arguments: dict, slots: tuple[Slot, ...], code: str, now: float, ttl: float) -> PendingIntent:
    return PendingIntent(tool, dict(arguments), slots, code, now + ttl, now)


def _complete(pending: PendingIntent) -> Proposal:
    args = {key: value for key, value in pending.arguments.items() if key != '_confirm' and not key.endswith('188')}
    if pending.purpose == 'scenario188':
        # NEO 188 NEO-SCENARIO-001: the answered input joins the values already accepted for this run.
        inputs = dict(args.pop('inputs', None) or {})
        for key in [k for k in args if k.startswith('input:')]:
            inputs[key[len('input:'):]] = args.pop(key)
        args['inputs'] = inputs
        args.pop('inputs_fingerprint', None)
        return pending.tool, args
    if pending.purpose == 'param188':
        # NEO 188 PARAM-06: the user chose WHICH timer; the new value was fixed before asking.
        timer_id, scale = str(pending.arguments['target188']).rsplit('|', 1)
        from jarvis.runtime.param_repair188 import new_seconds
        return 'timer', {'action': 'reschedule', 'id': timer_id, 'seconds': new_seconds(pending.arguments['_new188'], int(scale))}
    if pending.purpose == 'neo_model169' and '_neo169' in args:
        # NEO 169: the user chose one of the NEO command model's candidates.
        tool, arguments = (pending.guard157.get('neo169') or {})[str(args['_neo169'])]
        return str(tool), dict(arguments)
    if pending.tool == 'shelf' and args.get('action') == 'check' and isinstance(args.get('index'), int):
        args['index'] -= 1
    if pending.purpose == 'media_owner157':
        provider=args.pop('provider')
        if provider=='discord':
            args['action']={'next':'music_skip','previous':'music_previous','pause':'music_pause','play':'music_resume','stop':'music_stop'}[args['action']]
        return provider,args
    if pending.tool == 'open_request':
        kind, name = args.pop('kind'), args.pop('target')
        return ('application', {'action':'open','name':name}) if kind == 'application' else ('desktop', {'action':'open','query':name,'hint':kind})
    if pending.tool == 'filesystem' and args.get('action') == 'create_folder' and args.get('folder') and args.get('name'):
        folder=args.pop('folder');name=args.pop('name')
        from jarvis.desktop_resolver import FOLDER_ALIASES
        resolved = FOLDER_ALIASES.get(norm(folder), folder)
        args['destination']=ntpath.join(str(resolved),name) if '\\' in str(resolved) or re.match(r'^[A-Za-z]:',str(resolved)) else str(Path(resolved)/name)
    return pending.tool, args


def _partial(text: str, code: str, parse_complete: Callable[[str], Proposal | None], now: float, ttl: float) -> PendingIntent | None:
    from jarvis.runtime.command_contracts149 import missing_contract, parse_music_request, polite_tail
    value = polite_tail(strip_request_shell(text, code), code).strip(' .!?¿¡')
    i = LANGUAGES.index(code)
    from jarvis.runtime.volume188 import partial as volume188
    volume_missing = volume188(value, code)
    if volume_missing is not None:
        tool, args, slots = volume_missing
        return _new_pending(tool, args, tuple(Slot(*s) for s in slots), code, now, ttl)
    from jarvis.runtime.short_requests160 import partial as short160
    extra160 = short160(value, code)
    if extra160 is not None:
        tool, args, slots = extra160
        return _new_pending(tool, args, tuple(Slot(*s) for s in slots), code, now, ttl)
    from jarvis.runtime.file_requests162 import partial_file_request
    missing_file = partial_file_request(value, code)
    if missing_file is not None:
        tool, args, slots = missing_file
        return _new_pending(tool, args, tuple(Slot(*s) for s in slots), code, now, ttl)
    from jarvis.runtime.short_gaps160 import partial as gaps160
    missing160 = gaps160(value, code)
    if missing160 is not None:
        tool, args, slots = missing160
        return _new_pending(tool, args, tuple(Slot(*row) for row in slots), code, now, ttl)
    from jarvis.runtime.short_peripheral160 import partial as peripheral160
    peripheral_missing = peripheral160(value, code)
    if peripheral_missing is not None:
        tool, args, slots = peripheral_missing
        return _new_pending(tool, args, tuple(Slot(*row) for row in slots), code, now, ttl)
    explicit = parse_complete(value)
    if explicit and explicit[0] in {'music','yandex_music','spotify'} and explicit[1].get('action') in {'play_music','my_wave','my_tracks','play'}:
        return None  # existing selected-provider default is already complete
    if explicit and explicit[0]=='audio' and explicit[1].get('action')=='duck_system':
        return None  # the existing reversible attenuation has a defined default
    if explicit and explicit[0]=='discord' and explicit[1].get('action')=='bot_join_voice':
        from jarvis.runtime.command_contracts150 import short_request
        if short_request(value,code)==('discord','bot_join_voice'):
            # This established shorthand means join through Discord's existing
            # current-user/default-channel resolver. Named-channel requests keep
            # their typed slot and cannot silently choose a different channel.
            return None
    from jarvis.runtime.command_contracts150 import partial
    extra = partial(value, code)
    if extra is not None:
        tool, args, slots = extra
        return _new_pending(tool, args, tuple(Slot(*s) for s in slots), code, now, ttl)
    from jarvis.runtime.clarification_contracts156 import partial as partial156
    extra156 = partial156(value, code)
    # Preserve the dedicated object resolver for ordinary "open a file/folder".
    # It accepts an exact name and can ask among several real matches.
    for tool, action, field_name, kind, prompt, pattern in _heads(code):
        match = pattern.fullmatch(value)
        if tool == 'desktop' and match and not (match.group('tail') or '').strip() and not (extra156 and extra156[0]=='filesystem' and extra156[1].get('action')=='reveal'):
            return _new_pending(tool, {'action':action,'hint':prompt}, (Slot(field_name,kind,prompt),), code, now, ttl)
    if extra156 is not None:
        tool, args, slots = extra156
        return _new_pending(tool, args, tuple(Slot(*row) for row in slots), code, now, ttl)
    contract = missing_contract(value, code)
    if contract is not None:
        tool, args, slot = contract
        prompt = 'search_query' if args['action'] == 'search_track' else 'queue_query' if args['action'] in {'queue_add','queue_next'} else slot[2]
        return _new_pending(tool, args, (Slot(slot[0], slot[1], prompt),), code, now, ttl)
    musical = parse_music_request(value, code)
    if musical is not None and not musical[1].get('query'):
        return _new_pending(musical[0], musical[1], (Slot('query', 'name', 'search_query' if musical[1]['action'] == 'search_track' else 'query'),), code, now, ttl)
    payload = timer_payload(value, code)
    if payload is not None:
        if parse_duration(payload, code) is None:
            # NEO 188 PARAM-01: «таймер на двадцать» keeps the number and asks only for the unit.
            from jarvis.runtime.param_repair188 import bare_amount, amount_words
            amount = bare_amount(payload, code)
            if amount is not None and amount > 0:
                return _new_pending('timer', {'action':'timer','_amount188':amount}, (Slot('seconds','unit188','unit188',amount_words(payload, code)),), code, now, ttl)
            return _new_pending('timer', {'action':'timer'}, (Slot('seconds','duration','duration'),), code, now, ttl)
        return None
    if re.fullmatch(_OPEN[i], value, re.I):
        return _new_pending('open_request', {'action':'open'}, (Slot('kind','open_kind','open_kind'),Slot('target','name','target_for_kind')), code, now, ttl)
    if re.fullmatch(_NOTE[i], value, re.I):
        return _new_pending('shelf', {'action':'create','kind':'note'}, (Slot('title','name','title'),Slot('text','text','text')), code, now, ttl)
    if re.fullmatch(_SONG[i], value, re.I):
        return _new_pending('music', {'action':'play_track'}, (Slot('query','name','query'),), code, now, ttl)
    for tool, action, field_name, kind, prompt, pattern in _heads(code):
        match = pattern.fullmatch(value)
        if not match:
            continue
        tail = (match.group('tail') or '').strip()
        if tool == 'filesystem':
            from jarvis.runtime.clarification_contracts156 import foreign_head
            if foreign_head(value, code, tool):
                return None  # another domain owns this incomplete native request
        parsed = parse_complete(text) if tail else None
        if parsed:
            if parsed[0] != 'filesystem' or parsed[1].get('action') not in _MUTATING:
                return None
            args = dict(parsed[1])
            source = str(args.get('source',''))
            if not source or _absolute(source) or args.get('folder'):
                return None
            # A basename alone cannot authorise mutation of the first matching file.
            args.pop('source', None)
            return _new_pending('filesystem', args, (Slot('source','absolute_source','source',source),), code, now, ttl)
        if tool != 'filesystem' and tail:
            return None  # let the mature parser resolve complete requests
        args = {'action':action}
        slots = [Slot(field_name, kind, prompt)]
        if tool == 'desktop':
            args['hint'] = prompt
        if tool == 'filesystem':
            if tail:
                # Do not reinterpret an unparsed compound sentence as a filename.
                if re.search(r'\s(?:to|into|nach|dans|vers|para|в|и|and|und|puis)\s', tail, re.I):
                    return None
                source = _clean(tail, slots[0], code)
                if _absolute(source):
                    args['source'] = source
                else:
                    slots[0] = Slot('source','absolute_source','source',source)
            slots.append(Slot('new_name','filename','new_name') if action == 'rename' else Slot('destination','path','destination'))
        return _new_pending(tool, args, tuple(slots), code, now, ttl)
    return None


def _replacement(text: str, code: str) -> str:
    negative = {'ru':'не','en':'not','de':'nicht','fr':'pas','es':'no','it':'non','pt':'não'}[code]
    conjunction = {'ru':'а','en':'but','de':'sondern','fr':'mais','es':'sino','it':'ma','pt':'mas'}[code]
    found = re.fullmatch(negative + r'\s+.+?[, ]+' + conjunction + r'\s+(.+)', text, re.I)
    return found.group(1).strip() if found else text


_DECLINE184 = {
    'ru': r'^(?:нет[,.!\s]+)?(?:не\s+надо|не\s+нужно|не\s+хочу|не\s+стоит|не\s+открывай|не\s+запускай|ничего\s+не|отмен\w*|забудь|передумал\w*|ладно,?\s+не\s+надо|лучше\s+не)\b',
    'en': r'^(?:no[,.!\s]+)?(?:never\s*mind|forget\s+it|no\s+thanks|don[’\']t|do\s+not|cancel|not\s+now|skip\s+it|leave\s+it)\b',
    'de': r'^(?:nein[,.!\s]+)?(?:lieber\s+nicht|vergiss\s+es|nein\s+danke|nicht\s+nötig|öffne\s+nichts|starte\s+nichts|lass\s+(?:es|das)|abbrechen)\b',
    'fr': r'^(?:non[,.!\s]+)?(?:non\s+merci|laisse\s+tomber|n[’\']ouvre\s+rien|ne\s+lance\s+rien|pas\s+besoin|annule|oublie)\b|^non\s+merci\b',
    'es': r'^(?:no[,.!\s]+)?(?:mejor\s+no|déjalo|dejalo|no\s+abras|olvídalo|olvidalo|no\s+gracias|cancela)\b',
    'it': r'^(?:no[,.!\s]+)?(?:lascia\s+stare|lascia\s+perdere|non\s+aprire|no\s+grazie|lasciamo\s+stare|annulla)\b',
    'pt': r'^(?:não[,.!\s]+)?(?:deixa\s+estar|deixa\s+lá|esquece|não\s+abras|não\s+obrigad[oa]|cancela)\b',
}
_CHANGED_MIND184 = r'передумал|changed\s+my\s+mind|anders\s+überlegt|changé\s+d[’\']avis|cambiado\s+de\s+opinión|cambiato\s+idea|mudei\s+de\s+ideias?'
_SWITCH184 = {
    'ru': r'^(?:кстати|кстате|а\s+вообще|давай\s+(?:лучше\s+)?поговорим|поговорим)\b',
    'en': r'^(?:by\s+the\s+way|btw|anyway|let[’\']s\s+(?:just\s+)?talk)\b',
    'de': r'^(?:übrigens|uebrigens|lass\s+uns\s+(?:lieber\s+)?reden)\b',
    'fr': r'^(?:au\s+fait|d[’\']ailleurs|discutons)\b',
    'es': r'^(?:por\s+cierto|hablemos)\b',
    'it': r'^(?:a\s+proposito|parliamo)\b',
    'pt': r'^(?:já\s+agora|a\s+propósito|vamos\s+conversar)\b',
}


# NB42: «cancel shutdown» answering «which application?» is a literal name.
# A cancel word followed by its own object (not «it», «that», «please»…) names
# something; only a bare cancel word or one aimed at the request declines.
_CANCEL_WORD185 = r'^(?:cancel|abbrechen|annule[rz]?|cancela|annulla|отмени(?:ть)?|отмена)\s+(?P<rest>.+)$'
_CANCEL_OBJECT185 = (r'^(?:it|that|this|the\s+request|the\s+command|everything|all|please|request|opening|'
                     r'das|es|alles|bitte|ça|cela|tout|la\s+demande|lo|eso|todo|por\s+favor|tutto|questo|per\s+favore|'
                     r'isso|tudo|это|всё|все|пожалуйста|запрос|открытие|команду)\b')


def _literal_cancel_name185(low: str) -> bool:
    found = re.match(_CANCEL_WORD185, low)
    return bool(found and not re.match(_CANCEL_OBJECT185, found.group('rest').strip()))


def _slot_interruption184(text: str, code: str, slot: Slot | None = None) -> str:
    """'decline' / 'switch' / '' for a reply to an open «which application?» style question."""
    low = re.sub(r'\s+', ' ', str(text or '').casefold()).strip()
    if not low:
        return ''
    literal_name = slot is not None and slot.kind == 'name' and _literal_cancel_name185(low)
    if not literal_name and (re.search(_DECLINE184.get(code, _DECLINE184['en']), low) or re.search(_CHANGED_MIND184, low)):
        return 'decline'
    words_only = re.sub(r'\b(?:https?://|www\.)\S+', ' ', low)          # a link's «?» is not a question
    if (('?' in words_only or '¿' in words_only) and re.search(r'[^\W\d_]{2,}', words_only)) \
            or re.search(_SWITCH184.get(code, _SWITCH184['en']), low):
        return 'switch'
    from jarvis.runtime import meta_guard184
    if meta_guard184.classify(text, code) is not None:
        return 'switch'
    return ''


def _restore_literals184(original: str, repaired: str) -> str:
    """NEO 184: a typo repair fixes command words, never literal data.

    A word written as a label («enter: 25»), inside quotes or containing digits keeps its
    original spelling even when the repair changed it («merke dir enter: 25» keeps «enter»)."""
    if original == repaired:
        return repaired
    src, dst = str(original).split(), str(repaired).split()
    if len(src) != len(dst):
        return repaired
    out = []
    quoted = False
    for before, after in zip(src, dst):
        quoted_word = quoted or before[:1] in '«"“\''
        if before != after and (before.endswith(':') or quoted_word or any(ch.isdigit() for ch in before)):
            out.append(before)
        else:
            out.append(after)
        if before[:1] in '«"“':
            quoted = True
        if before[-1:] in '»"”':
            quoted = False
    return ' '.join(out)


def _provider(text: str) -> str:
    value = norm(text)
    value = re.sub(r'^(?:в|на|in|on|bei|dans|sur|en|nel|no|em)\s+', '', value)
    if value in {'яндекс','яндекс музыка','яндекс музыку','яндекс музыке','яндекс мьюзик','yandex','yandex music'}:
        return 'yandex_music'
    if value in {'spotify','спотифай','спотифи'}:
        return 'spotify'
    return ''


def command_identity(candidate: str, code: str, parse_complete: Callable[[str], Proposal | None], now: float, ttl: float = 60):
    """Typed route of a complete or slot-missing command, or None. Never executes."""
    short = _partial(candidate, code, parse_complete, now, ttl)
    if short is not None:
        return (short.tool, short.arguments, tuple((x.name,x.kind) for x in short.slots))
    from jarvis.runtime.short_requests160 import complete as complete160
    return parse_complete(candidate) or complete160(candidate,code)


def prepare_dialogue(text: str, code: str, state: Any, parse_complete: Callable[[str], Proposal | None], *, pending_confirmation: bool = False, now: float | None = None, ttl: float = 60, correction_ttl: float = 120, folder_context: Callable[[], list[dict]] | None = None, timer_context: Callable[[], list[dict] | None] | None = None) -> DialogueDecision:
    """One bounded turn; never confirms a different source's or expired request."""
    now = time.monotonic() if now is None else now
    if code not in LANGUAGES:
        return DialogueDecision(text)
    # Text/name/path follow-ups are literal. Repair only declared fixed command
    # words/spaces in a fresh request; the existing grammar owns every result.
    if getattr(state, 'pending_intent', None) is None and not pending_confirmation:
        from jarvis.runtime.knowledge_phrases188 import parse as revoke188
        revoke = revoke188(text, code)
        if revoke is not None:
            # NEO 188: «suche nicht mehr im Ordner X» is a revocation, not a refused search.
            return DialogueDecision(text, feature=revoke, purpose='command')
        from jarvis.runtime.param_repair188 import unit_scale
        if unit_scale(text, code) is not None:
            # NEO 188 PARAM-07: a lone «минут» answers no open question; never fuzz it into a command.
            return DialogueDecision(text, message('no_context', code), purpose='clarification')
        from jarvis.runtime.command_repair160 import repair, descriptive_statement
        if descriptive_statement(text,code):
            return DialogueDecision(text, message('reported',code), purpose='reported')
        def proposal(candidate):
            return command_identity(candidate, code, parse_complete, now, ttl)
        from jarvis.runtime.command_repair160 import command_after_greeting
        text=command_after_greeting(text,code,proposal)
        from jarvis.runtime.command_repair161 import repair as repair_literals161
        repaired, alternatives = repair_literals161(text, code, proposal)
        if repaired == text and not alternatives:
            repaired, alternatives = repair(text, code, proposal)
        if (repaired == text and not alternatives
                and classify_utterance(text, code).purpose not in {'reported', 'retrospective'}):
            # Keyboard/phonetic word typos outside the literal templates. Reported
            # speech is never parsed; a result must be a command the grammar accepts.
            from jarvis.runtime.command_typos165 import repair as repair_typos165
            repaired, alternatives = repair_typos165(text, code, proposal)
        from jarvis.runtime.typos175 import polarity_flip188
        if polarity_flip188(text, repaired):
            repaired = text                       # NEO 188 atlas: «riattiva» is never repaired into «disattiva»
        alternatives = [x for x in (alternatives or []) if not polarity_flip188(text, x)]
        if alternatives:
            pending = _new_pending('command_choice160', {}, (Slot('command','choice','choose',choices=tuple((x,x) for x in alternatives)),),code,now,ttl)
            state.pending_intent=pending
            return DialogueDecision(text, pending.question(), purpose='clarification')
        text=_restore_literals184(text, repaired)
    utterance = classify_utterance(text, code)
    purpose = utterance.purpose
    value = _replacement(utterance.text, code) if purpose == 'correction' else text
    pending = getattr(state, 'pending_intent', None)
    if (pending is not None and pending.language == code and now <= pending.expires_at
            and pending.missing() is not None and pending.missing().kind == 'text' and purpose != 'cancel'):
        purpose, value = 'unknown', text  # requested content is literal data, including correction words
    if pending is not None and pending.missing() is not None and pending.missing().kind != 'text' and norm(text) in {'стоп','stop','stopp','arrête','arrete','detén','deten','ferma','pare'}:
        purpose='cancel'
    help_questions = {
        'ru': {'что ты ждёшь', 'что тебе нужно', 'повтори вопрос', 'какие данные нужны', 'почему ты спрашиваешь'},
        'en': {'what do you need', 'what are you waiting for', 'repeat the question', 'which details do you need', 'why are you asking'},
        'de': {'was brauchst du', 'worauf wartest du', 'wiederhole die Frage', 'welche Angaben brauchst du'},
        'fr': {'que te faut-il', 'qu’attends-tu', 'répète la question', 'quelles précisions te faut-il'},
        'es': {'qué necesitas', 'qué estás esperando', 'repite la pregunta', 'qué datos necesitas'},
        'it': {'cosa ti serve', 'cosa stai aspettando', 'ripeti la domanda', 'quali dati ti servono'},
        'pt': {'do que precisas', 'o que estás à espera', 'repete a pergunta', 'que dados precisas'},
    }
    if (pending is not None and pending.language == code and now <= pending.expires_at
            and pending.missing() is not None and pending.missing().kind != 'text'
            and norm(text) in {norm(x) for x in help_questions[code]}):
        return DialogueDecision(text, pending.question(), purpose='clarification')
    if (pending is not None and pending.language == code and now <= pending.expires_at and not pending_confirmation
            and pending.missing() is not None and pending.missing().kind not in {'text', 'confirm', 'calls157'}
            and pending.purpose not in {'neo_model169'} and not _quoted(text.strip())):
        # NEO 188 NEO-UX-002: «Что такое полный путь?» explains the open field and repeats the SAME question;
        # nothing is filled, confirmed or started, and the TTL is not extended.
        from jarvis.runtime.field_help188 import explain
        help188 = explain(text, code, pending.missing().kind, pending.missing().prompt)
        if help188 is not None:
            return DialogueDecision(text, help188 + '\n' + pending.question(), purpose='clarification')
    if purpose in {'retrospective','reported'}:
        if purpose == 'reported':
            return DialogueDecision(text, message('reported',code), purpose=purpose)
        attempt = getattr(state, 'last_attempt', {}) or {}
        reply = message('retrospective',code,result=attempt['text']) if attempt.get('text') else message('retrospective_empty',code)
        return DialogueDecision(text, reply, purpose=purpose)
    if purpose == 'cancel' and (pending is not None or pending_confirmation):
        state.pending_intent = None
        return DialogueDecision(text, message('cancelled',code), purpose='cancelled', cancel_confirmation=True)
    if pending_confirmation and purpose == 'correction':
        state.pending_intent = None
        # A full corrected command can re-enter ordinary risk checks. A partial
        # answer must not accidentally confirm the old destructive proposal.
        if value and (parse_complete(value) or looks_like_new_command(value,code)):
            return DialogueDecision(value, purpose='correction', cancel_confirmation=True)
        return DialogueDecision(text, message('confirmation_reset',code), purpose='clarification', cancel_confirmation=True)
    if pending is not None:
        if pending.language != code:
            state.pending_intent = None
            pending = None
        elif now > pending.expires_at:
            state.pending_intent = None
            if not looks_like_new_command(value,code) and parse_complete(value) is None:
                return DialogueDecision(text, message('expired',code), purpose='clarification_expired')
            pending = None
    if pending is not None:
        slot = pending.missing()
        if slot is None:
            state.pending_intent = None
        else:
            if purpose == 'correction' and not value:
                return DialogueDecision(text, pending.question(), purpose='clarification')
            # NEO 184: a refusal, a question or a change of topic answers no slot: the open request
            # is dropped (a refusal is acknowledged; a question goes on as ordinary conversation).
            if slot.kind not in {'text', 'calls157'} and not _quoted(value):
                interruption = _slot_interruption184(text, code, slot)
                if interruption == 'decline':
                    state.pending_intent = None
                    return DialogueDecision(text, message('cancelled', code), purpose='cancelled')
                if interruption == 'switch':
                    state.pending_intent = None
                    return DialogueDecision(text, purpose='conversation')
            # Text slots explicitly take the next sentence as DATA. Other slots
            # can be interrupted by a new complete command, never by a filename
            # merely containing an action word (or by a quoted literal payload).
            from jarvis.runtime.social_atlas import catalog as social_catalog
            if slot.kind != 'text' and not _quoted(value) and norm(value) in social_catalog(code):
                family = social_catalog(code)[norm(value)][1]
                if family not in {'thanks', 'okay', 'apology'}:
                    state.pending_intent = None
                return DialogueDecision(text)  # courtesy can acknowledge the question; a new topic replaces it
            if slot.kind == 'unit188':
                answered188 = _unit_answer188(pending, slot, text, value, code, state, now, ttl)
                if answered188 is not None:
                    return answered188
            typed_value = _validate(slot,value,code) if slot.kind.startswith(('number157:','int157:','scenario188:')) or slot.kind in {'bool157','mouse_button157','date157','hotkey157','choice'} else None
            if slot.kind not in {'text','calls157'} and typed_value is None and not _quoted(value) and (parse_complete(value) is not None or looks_like_new_command(value, code) or _explicit_media186(value, code)):
                state.pending_intent = None
            else:
                if slot.kind == 'calls157':
                    from jarvis.runtime.clarification_contracts157 import parse_calls
                    validated = parse_calls(value,code,parse_complete,action=pending.arguments.get('action','queue'))
                else:
                    validated = _validate(slot, value, code)
                if validated is None:
                    reply = message('bad_duration',code) if slot.kind == 'duration' else message('invalid',code,question=pending.question())
                    return DialogueDecision(text, reply, purpose='clarification')
                if pending.tool == 'search' and pending.arguments.get('action') == 'files' and slot.name == 'query' and _quoted(value):
                    pending.arguments['literal'] = True
                pending.arguments[slot.name] = validated
                pending.expires_at = now + ttl
                if pending.missing():
                    return DialogueDecision(text, pending.question(), purpose='clarification')
                state.pending_intent = None
                if pending.tool == 'command_choice160':
                    return prepare_dialogue(validated, code, state, parse_complete, now=now, ttl=ttl, correction_ttl=correction_ttl, folder_context=folder_context, timer_context=timer_context)
                if pending.tool == 'object_request157':
                    from jarvis.runtime.object_context157 import bind
                    return bind(json.loads(pending.arguments['object']),pending.arguments['action'],text,code,state,now,ttl)
                from jarvis.runtime.object_context157 import valid_guard, changed_message
                if not valid_guard(pending.guard157):
                    return DialogueDecision(text,changed_message(code),purpose='clarification')
                feature = _complete(pending)
                if pending.purpose == 'param188':
                    _remember_units188(state, feature[1].get('seconds'), int(str(pending.arguments['target188']).rsplit('|', 1)[1]), now)
                return DialogueDecision(text, feature=feature, purpose=pending.purpose, from_context=True,guard157=pending.guard157)
    if getattr(state, 'pending_intent', None) is None and not pending_confirmation:
        corrected188 = _param_correction188(text, code, state, now, correction_ttl, timer_context)
        if corrected188 is not None:
            return corrected188
    if purpose == 'correction':
        if not value:
            return DialogueDecision(text, message('correction_missing',code), purpose='clarification')
        if negated_action(value, code):
            return DialogueDecision(value, purpose='negation')
        # A fully specified replacement is still parsed and checked normally.
        explicit = parse_complete(value)
        if explicit is not None:
            return DialogueDecision(value, feature=explicit, purpose='correction')
        attempt = getattr(state, 'last_attempt', {}) or {}
        if not attempt or now - float(attempt.get('at', 0)) > correction_ttl:
            # NEO 184: «Я имел в виду закат. Почему он красный?» corrects the conversation, not a command.
            if '?' in text or re.search(r'[.!]\s+\S', text):
                return DialogueDecision(text, purpose='conversation')
            return DialogueDecision(text, message('no_context',code), purpose='clarification')
        tool, args = str(attempt.get('tool','')), dict(attempt.get('arguments') or {})
        action = str(args.get('action',''))
        if tool == 'timer' and action in {'timer','reschedule'}:
            seconds = parse_duration(value, code)
            timer_id = str((attempt.get('data') or {}).get('id',''))
            if seconds is None:
                bare188 = _bare_timer_correction188(value, code, state, attempt, timer_id, now, ttl)
                if bare188 is not None:
                    return bare188
                return DialogueDecision(text, message('bad_duration',code), purpose='clarification')
            if attempt.get('success') and timer_id:
                return DialogueDecision(text, feature=('timer', {'action':'reschedule','id':timer_id,'seconds':seconds}), purpose='correction',from_context=True)
            if not attempt.get('success'):
                return DialogueDecision(text, feature=('timer', {'action':'timer','seconds':seconds}), purpose='correction',from_context=True)
        if attempt.get('success') and ((tool == 'audio' and action == 'set_volume') or (tool in {'music','yandex_music','spotify'} and action == 'set_volume')):
            from jarvis.runtime.param_repair188 import bare_amount, text as param_text
            level = bare_amount(value, code)
            if level is not None and float(level).is_integer():
                if not 0 <= level <= 100:
                    return DialogueDecision(text, param_text('volume_range', code), purpose='clarification')
                corrected = dict(args); corrected['value' if tool == 'audio' else 'volume'] = int(level)
                return DialogueDecision(text, feature=(tool, corrected), purpose='correction', from_context=True)
        provider = _provider(value)
        if tool in {'music','yandex_music','spotify'} and provider and action in {'play','pause','stop','next','previous','play_track','play_song','play_music','my_tracks','my_wave'}:
            # Provider-specific unsupported actions remain subject to schema checks.
            try:
                args = {k:v for k,v in args.items() if k in {'action','query','playlist','volume','step'}}
                if action in {'my_wave','my_tracks'} and provider == 'spotify':
                    return DialogueDecision(text, message('correction_missing',code), purpose='clarification')
                label = 'Яндекс Музыке' if code == 'ru' and provider == 'yandex_music' else 'Yandex Music' if provider == 'yandex_music' else 'Spotify'
                correction = _new_pending(provider,args,(Slot('_confirm','confirm','confirm_correction',label),),code,now,ttl)
                correction.purpose = 'correction'
                state.pending_intent = correction
                return DialogueDecision(text, correction.question(), purpose='clarification')
            except (TypeError, ValueError):
                return DialogueDecision(text, message('correction_missing',code), purpose='clarification')
        if tool == 'application' and action == 'open' and not attempt.get('success'):
            name = _validate(Slot('name','name','application'), value, code)
            if name is not None:
                return DialogueDecision(text,feature=('application',{'action':'open','name':name}),purpose='correction',from_context=True)
        return DialogueDecision(text,message('completed_correction' if attempt.get('success') else 'correction_missing',code),purpose='clarification')
    if not negated_action(value, code):
        from jarvis.runtime.short_requests160 import bare_browser
        browser=bare_browser(value,code)
        if browser is not None:
            return DialogueDecision(value, feature=browser, purpose='command')
        from jarvis.runtime.object_context157 import resolve as resolve_object157
        object_decision = resolve_object157(value, code, state, now, ttl)
        if object_decision is not None:
            return object_decision
        from jarvis.runtime.clarification_contracts156 import folder_close_request
        if folder_context is not None and folder_close_request(value, code):
            try:
                from jarvis.runtime.folder_context156 import encode
                rows = folder_context()
                if not rows:
                    return DialogueDecision(text, message('folder_none156', code), purpose='clarification')
                if len(rows) == 1:
                    return DialogueDecision(text, feature=('desktop', {'action':'close_folder', 'query':encode(rows[0]), 'hint':'folder'}), purpose='command')
                # A bounded menu retains the exact identities originally shown.
                if len(rows) > 40:
                    return DialogueDecision(text, message('window156', code), purpose='clarification')
                titles = [row['title'] for row in rows]
                choices = tuple((row['title'] if titles.count(row['title']) == 1 else row['title'] + ' [' + str(i) + ']', encode(row)) for i, row in enumerate(rows, 1))
                pending = _new_pending('desktop', {'action':'close_folder','hint':'folder'}, (Slot('query','choice','folder_choose156',choices=choices),), code, now, ttl)
                state.pending_intent = pending
                return DialogueDecision(text, pending.question(), purpose='clarification')
            except (OSError, ValueError, KeyError, TypeError, ImportError):
                return DialogueDecision(text, message('folder_unavailable156', code), purpose='clarification')
        partial = _partial(value, code, parse_complete, now, ttl)
        if partial is not None:
            state.pending_intent = partial
            return DialogueDecision(text, partial.question(), purpose='clarification')
    if not negated_action(value, code) and parse_complete(value) is None:
        from jarvis.runtime.short_requests160 import complete as complete160
        completed=complete160(value,code)
        if completed is not None:
            return DialogueDecision(value, feature=completed, purpose='command')
    return DialogueDecision(value)


def record_attempt(state: Any, tool: str, arguments: dict, execution: Any, *, now: float | None = None) -> None:
    """RAM-only bounded evidence, including failures. No fabricated success."""
    data = execution.data if isinstance(getattr(execution,'data',None),dict) else {}
    from jarvis.runtime.object_context157 import remember
    remember(state, tool, arguments, execution, now)
    if getattr(execution,'success',False) and tool in {'music','yandex_music','spotify','discord','media'}:
        action=arguments.get('action','')
        playback={'play','play_track','play_song','play_music','my_wave','my_tracks','playlist','next','previous','pause','stop','play_search','play_pause'}
        discord_playback={'music_bot_play','music_youtube_url','music_my_wave','music_my_tracks','music_playlist','music_skip','music_previous','music_pause','music_resume','music_stop'}
        if (tool=='discord' and action in discord_playback) or (tool!='discord' and action in playback) or (tool!='discord' and (data.get('playing') or data.get('paused'))):
            state.media_owner157={'tool':tool,'arguments':dict(arguments),'at':time.monotonic() if now is None else now,'language':getattr(state,'language','ru')}
    keep = ('id','name','title','artist','path','kind','provider','verified','completed','playing','paused','outcome')
    state.last_attempt = {'tool':tool, 'arguments':dict(arguments), 'success':bool(getattr(execution,'success',False)),
                          'outcome':str(getattr(execution,'outcome','')), 'text':str(getattr(execution,'text',''))[:2400],
                          'data':{k:data[k] for k in keep if k in data}, 'at':time.monotonic() if now is None else now}
    if len(json.dumps(state.last_attempt,ensure_ascii=False,default=str).encode('utf-8')) > 32768:
        state.last_attempt['arguments'] = {k:str(v)[:200] for k,v in arguments.items()}
    if tool == 'timer' and arguments.get('action') in {'timer', 'reschedule'}:
        # NEO 188: remember the unit the user actually chose for THIS timer id only.
        hint = getattr(state, 'timer_units188', None) or {}
        timer_id = str(data.get('id', '') or arguments.get('id', ''))
        try:
            same = abs(float(hint.get('seconds', -1)) - float(arguments.get('seconds', -2) or -2)) <= 0.5
        except (TypeError, ValueError):
            same = False
        if getattr(execution, 'success', False) and timer_id and same and hint.get('scale'):
            state.timer_scale188 = {'id': timer_id, 'scale': int(hint['scale'])}
        elif getattr(execution, 'success', False) and arguments.get('action') == 'timer':
            state.timer_scale188 = {}
        state.timer_units188 = {}
    # Exact options from the actual resolver, not invented candidates. Do not
    # repeat uncertain operations that may already have executed.
    if data.get('requires_clarification') and not data.get('requires_confirmation_of_result') and tool == 'desktop' and arguments.get('action') == 'open':
        options = data.get('options')
        if isinstance(options,list) and 1 < len(options) <= 8 and all(isinstance(p,str) and _absolute(p) for p in options):
            args = dict(arguments); args.pop('query',None)
            state.pending_intent = _new_pending(tool,args,(Slot('query','choice','choose',choices=tuple((p,p) for p in options)),), getattr(state,'language','ru'),time.monotonic() if now is None else now,60)
    if data.get('requires_clarification') and tool == 'knowledge' and arguments.get('action') == 'remove_folder':
        # NEO 188 NEO-PRIVACY-001: the numbered choice is bound to the exact full paths shown.
        options = data.get('options')
        if isinstance(options, list) and 1 < len(options) <= 8 and all(isinstance(p, str) and p for p in options):
            state.pending_intent = _new_pending('knowledge', {'action': 'remove_folder'},
                                                (Slot('folder', 'choice', 'choose', choices=tuple((p, p) for p in options)),),
                                                getattr(state, 'language', 'ru'), time.monotonic() if now is None else now, 60)



def _explicit_media186(value: str, code: str) -> bool:
    """VIDEO185-PENDING-STOP-SOURCE-LEAD: an explicit media/video command («стоп
    видео», «продолжи видео», «next song») supersedes a pending choice instead of
    being rejected as an invalid answer. Literal answers are not commands."""
    try:
        from jarvis.runtime.media_grammar186 import canonical_media_command
        from jarvis.runtime.media_phrases import parse_explicit_media_phrase
        from jarvis.runtime.multilingual_commands import normalize_multilingual_command
        if canonical_media_command(value, code):
            return True
        normalized = normalize_multilingual_command(value, code).casefold().replace('ё', 'е').strip(' .!?')
        intent = parse_explicit_media_phrase(normalized)
        return intent is not None and not intent.query
    except Exception:
        return False


# ---------------------------------------------------------------------------
# NEO 188 NEO-UX-001 / TZ-PARAMETER-CLARIFICATION-CORRECTION-01
# ---------------------------------------------------------------------------

def _remember_units188(state: Any, seconds: Any, scale: Any, now: float) -> None:
    try:
        state.timer_units188 = {'seconds': float(seconds), 'scale': int(scale) if scale else None, 'at': now}
    except (TypeError, ValueError, AttributeError):
        pass


def _finish_unit188(pending: PendingIntent, text: str, seconds: float, scale: int | None, code: str, state: Any, now: float) -> DialogueDecision:
    from jarvis.runtime.param_repair188 import seconds_in_range
    if not seconds_in_range(seconds):
        return DialogueDecision(text, message('bad_duration', code), purpose='clarification')
    state.pending_intent = None
    _remember_units188(state, seconds, scale, now)
    args = {key: value for key, value in pending.arguments.items() if not key.endswith('188')}
    args['seconds'] = float(seconds)
    return DialogueDecision(text, feature=('timer', args), purpose=pending.purpose, from_context=True)


def _unit_answer188(pending: PendingIntent, slot: Slot, text: str, value: str, code: str, state: Any, now: float, ttl: float) -> DialogueDecision | None:
    """PARAM-01/03: the unit (or a corrected number) for the SAME pending timer."""
    from jarvis.runtime import param_repair188 as pr
    amount = float(pending.arguments.get('_amount188') or 0)
    pair = pr.not_x_y(text, code)
    if pair is not None:
        old, new, old_text = pair
        if old.get('amount') is None or abs(float(old['amount']) - amount) > 1e-9:
            label = pr.amount_label(amount, slot.hint, code)
            return DialogueDecision(text, pr.text('pending_mismatch', code, current=label, old=old_text, question=pending.question()),
                                    purpose='clarification')
        if new.get('seconds') is not None:
            return _finish_unit188(pending, text, new['seconds'], new.get('scale'), code, state, now)
        if old.get('scale'):
            return _finish_unit188(pending, text, float(new['amount']) * old['scale'], old['scale'], code, state, now)
        pending.arguments['_amount188'] = float(new['amount'])
        pending.slots = tuple(Slot(s.name, s.kind, s.prompt, str(new.get('raw') or ''), s.choices) if s is slot else s for s in pending.slots)
        return DialogueDecision(text, pending.question(), purpose='clarification')
    scale = pr.unit_scale(value, code)
    if scale is not None:
        return _finish_unit188(pending, text, amount * scale, scale, code, state, now)
    quantity = pr.quantity(value, code)
    if quantity is not None and quantity.get('seconds') is not None:
        return _finish_unit188(pending, text, quantity['seconds'], quantity.get('scale'), code, state, now)
    if quantity is not None and quantity.get('amount'):
        # Another bare number replaces the number; the question about the unit stays.
        pending.arguments['_amount188'] = float(quantity['amount'])
        pending.slots = tuple(Slot(s.name, s.kind, s.prompt, pr.amount_words(value, code), s.choices) if s is slot else s for s in pending.slots)
        return DialogueDecision(text, pending.question(), purpose='clarification')
    return None


def _active_timers188(timer_context: Callable[[], list[dict] | None] | None) -> list[dict] | None:
    if timer_context is None:
        return None
    try:
        rows = timer_context()
    except Exception:
        return None
    if rows is None:
        return None
    return [dict(row) for row in rows if str(row.get('kind', 'timer')) == 'timer' and row.get('id')]


def _param_correction188(text: str, code: str, state: Any, now: float, correction_ttl: float,
                         timer_context: Callable[[], list[dict] | None] | None) -> DialogueDecision | None:
    """PARAM-02/04/06: «не двадцать, двенадцать», «не 50, 30» for the last operation."""
    from jarvis.runtime import param_repair188 as pr
    pair = pr.not_x_y(text, code)
    attempt = getattr(state, 'last_attempt', {}) or {}
    if pair is None or not attempt or now - float(attempt.get('at', 0) or 0) > correction_ttl:
        return None
    old, new, old_text = pair
    tool, args = str(attempt.get('tool', '')), dict(attempt.get('arguments') or {})
    action = str(args.get('action', ''))
    if tool == 'timer' and action in {'timer', 'reschedule'} and attempt.get('success'):
        last_id = str((attempt.get('data') or {}).get('id', '') or args.get('id', ''))
        rows = _active_timers188(timer_context)
        if rows is None:
            rows = [{'id': last_id, 'seconds': float(args.get('seconds') or 0), 'label': ''}] if last_id else []
        from jarvis.runtime.duration import format_duration
        matches = [(row, scale) for row in rows for scale in pr.old_matches(old, float(row.get('seconds') or 0))]
        if not matches:
            current = next((row for row in rows if str(row['id']) == last_id), None)
            if current is None:
                return DialogueDecision(text, pr.text('timer_none', code, old=old_text), purpose='clarification')
            return DialogueDecision(text, pr.text('timer_mismatch', code, current=format_duration(float(current['seconds']), code), old=old_text),
                                    purpose='clarification')
        candidates = [(row, scale, pr.new_seconds(new, scale)) for row, scale in matches]
        candidates = [item for item in candidates if pr.seconds_in_range(item[2])]
        if not candidates:
            return DialogueDecision(text, message('bad_duration', code), purpose='clarification')
        if len(candidates) == 1:
            row, scale, seconds = candidates[0]
            _remember_units188(state, seconds, new.get('scale') or scale, now)
            return DialogueDecision(text, feature=('timer', {'action': 'reschedule', 'id': str(row['id']), 'seconds': seconds}),
                                    purpose='correction', from_context=True)
        choices = tuple(((str(row.get('label') or '') or format_duration(float(row['seconds']), code)) + ' · ' + format_duration(float(row['seconds']), code),
                         f"{row['id']}|{scale}") for row, scale, _ in candidates[:8])
        pending = _new_pending('timer', {'action': 'reschedule', '_new188': new}, (Slot('target188', 'choice', 'which_timer188', choices=choices),), code, now, 60)
        pending.purpose = 'param188'
        state.pending_intent = pending
        return DialogueDecision(text, pending.question(), purpose='clarification')
    if attempt.get('success') and action == 'set_volume' and tool in {'audio', 'music', 'yandex_music', 'spotify'}:
        key = 'value' if tool == 'audio' else 'volume'
        if old.get('amount') is None or new.get('amount') is None or old.get('scale') or new.get('scale'):
            return None
        try:
            current = int(float(args.get(key)))
        except (TypeError, ValueError):
            return None
        if int(old['amount']) != current or not float(old['amount']).is_integer():
            return DialogueDecision(text, pr.text('volume_mismatch', code, current=current, old=old_text), purpose='clarification')
        level = float(new['amount'])
        if not level.is_integer() or not 0 <= level <= 100:
            return DialogueDecision(text, pr.text('volume_range', code), purpose='clarification')
        corrected = dict(args)
        corrected[key] = int(level)
        return DialogueDecision(text, feature=(tool, corrected), purpose='correction', from_context=True)
    return None


def _bare_timer_correction188(value: str, code: str, state: Any, attempt: dict, timer_id: str, now: float, ttl: float) -> DialogueDecision | None:
    """«нет, двенадцать»: the unit the user chose for this timer, or ask for it."""
    from jarvis.runtime import param_repair188 as pr
    amount = pr.bare_amount(value, code)
    if amount is None or amount <= 0:
        return None
    remembered = getattr(state, 'timer_scale188', None) or {}
    if attempt.get('success') and timer_id and str(remembered.get('id', '')) == timer_id and remembered.get('scale'):
        seconds = amount * int(remembered['scale'])
        if not pr.seconds_in_range(seconds):
            return DialogueDecision(value, message('bad_duration', code), purpose='clarification')
        _remember_units188(state, seconds, remembered['scale'], now)
        return DialogueDecision(value, feature=('timer', {'action': 'reschedule', 'id': timer_id, 'seconds': seconds}), purpose='correction', from_context=True)
    args = {'action': 'reschedule', 'id': timer_id} if attempt.get('success') and timer_id else {'action': 'timer'}
    if not attempt.get('success') and timer_id:
        return None
    args['_amount188'] = amount
    pending = _new_pending('timer', args, (Slot('seconds', 'unit188', 'unit188', pr.amount_words(value, code)),), code, now, ttl)
    state.pending_intent = pending
    return DialogueDecision(value, pending.question(), purpose='clarification')
