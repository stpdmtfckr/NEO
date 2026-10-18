"""Reviewed literal missing-slot heads. No fuzzy payload or command execution."""
from __future__ import annotations
from functools import lru_cache
import json
from pathlib import Path
import re

from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell
from jarvis.runtime.clarification_contracts157 import slot

QUESTIONS = {
    'city': ('Для какого города?', 'For which city?', 'Für welche Stadt?', 'Pour quelle ville ?', '¿Para qué ciudad?', 'Per quale città?', 'Para que cidade?'),
    'language': ('На какой язык перевести?', 'Which language should I translate into?', 'In welche Sprache soll ich übersetzen?', 'Dans quelle langue traduire ?', '¿A qué idioma traduzco?', 'In quale lingua traduco?', 'Para que língua devo traduzir?'),
    'instruction': ('Что именно разобрать или объяснить на экране?', 'What should I examine or explain on the screen?', 'Was soll ich auf dem Bildschirm untersuchen oder erklären?', 'Que dois-je examiner ou expliquer à l’écran ?', '¿Qué examino o explico en la pantalla?', 'Cosa devo esaminare o spiegare sullo schermo?', 'O que devo analisar ou explicar no ecrã?'),
    'checklist': ('Какой чек-лист на полке? Назови его заголовок.', 'Which shelf checklist? Tell me its title.', 'Welche Regalliste? Nenne ihren Titel.', 'Quelle liste de l’étagère ? Indique son titre.', '¿Qué lista de la estantería? Dime su título.', 'Quale lista dello scaffale? Dimmi il titolo.', 'Qual lista da estante? Indica o título.'),
}


def question(prompt, code):
    return QUESTIONS[prompt.split(':', 1)[1]][LANGUAGES.index(code)]


CONTRACTS = {
    'screen.analyze': ({'action': 'analyze'}, (slot('instruction', 'text', 'peripheral160:instruction'),)),
    'shelf.check': ({'action': 'check', 'checked': True}, (slot('title', prompt='peripheral160:checklist'), slot('index', 'int157:1:10000', 'item_index'))),
    'shelf.create': ({'action': 'create', 'kind': 'note'}, (slot('title', prompt='title'), slot('text', 'text', 'text'))),
    'filesystem.create_folder': ({'action': 'create_folder'}, (slot('folder', 'path', 'destination'), slot('name', 'filename', 'new_name'))),
    'filesystem.reveal': ({'action': 'reveal'}, (slot('source', 'absolute_source', 'file_path156'),)),
    'overlay.checklist_check': ({'action': 'checklist_check'}, (slot('name', prompt='panel_name'), slot('item', 'positive_integer', 'item_index'))),
    'overlay.checklist_complete_all': ({'action': 'checklist_complete_all'}, (slot('name', prompt='panel_name'),)),
    'overlay.checklist_uncheck_all': ({'action': 'checklist_uncheck_all'}, (slot('name', prompt='panel_name'),)),
    'timer.alarm': ({'action': 'alarm'}, (slot('text', 'clock', 'alarm_time'),)),
    'window_advanced.focus_instance': ({'action': 'focus_instance'}, (slot('query', prompt='application'), slot('index', 'int157:1:50', 'item_index'))),
    'mini_browser.open': ({'action': 'open'}, (slot('url', 'url', 'url'),)),
    'translate.translate': ({}, (slot('target_language', prompt='peripheral160:language'), slot('text', 'text', 'text'))),
}
for _action in ('summary', 'rain', 'sunrise', 'sunset'):
    CONTRACTS['weather.' + _action] = ({'action': _action}, (slot('city', prompt='peripheral160:city'),))

# Unambiguous choices re-enter the normal clarification parser. They are never
# sent to tools and do not smuggle invented names or prepositions as arguments.
EXPLICIT = {
    'shelf.check': ('отметь пункт чек-листа на полке', 'check a shelf checklist item', 'hake einen Punkt der Regalliste ab', 'coche un élément de la liste de l’étagère', 'marca un punto de la lista de la estantería', 'spunta una voce della lista dello scaffale', 'marque um item da lista da estante'),
    'overlay.checklist_check': ('отметь пункт списка панели', 'check an item on a panel', 'hake einen Punkt auf einer Anzeige ab', 'coche un élément sur un panneau', 'marca un punto en un panel', 'spunta una voce su un pannello', 'marque um item num painel'),
    'overlay.checklist_complete_all': ('отметь выполненными все пункты панели', 'check all items on a panel', 'hake alle Punkte einer Anzeige ab', 'coche tous les éléments d’un panneau', 'marca todos los puntos de un panel', 'spunta tutte le voci di un pannello', 'marque todos os itens de um painel'),
    'overlay.checklist_uncheck_all': ('сними все отметки пунктов панели', 'uncheck all items on a panel', 'entferne alle Häkchen einer Anzeige', 'décoche tous les éléments d’un panneau', 'desmarca todos los puntos de un panel', 'togli tutte le spunte di un pannello', 'desmarque todos os itens de um painel'),
    'filesystem.reveal': ('покажи расположение файла в проводнике', 'show a file location in Explorer', 'zeige den Dateispeicherort im Explorer', 'montre l’emplacement du fichier dans l’explorateur', 'muestra la ubicación del archivo en el explorador', 'mostra la posizione del file in Esplora file', 'mostra a localização do ficheiro no explorador'),
    'filesystem.open': ('открой файл', 'open file', 'öffne die Datei', 'ouvre le fichier', 'abre el archivo', 'apri il file', 'abre o ficheiro'),
    'weather.sunrise': ('восход солнца для города', 'sunrise for a city', 'Sonnenaufgang für die Stadt', 'lever du soleil pour une ville', 'salida del sol para una ciudad', 'alba per una città', 'nascer do sol para uma cidade'),
    'weather.sunset': ('закат солнца для города', 'sunset for a city', 'Sonnenuntergang für die Stadt', 'coucher du soleil pour une ville', 'puesta del sol para una ciudad', 'tramonto per una città', 'pôr do sol para uma cidade'),
}


def _contract(intent):
    args, slots = CONTRACTS[intent]
    return intent.split('.', 1)[0], dict(args), slots


def _choice(intents, code):
    i = LANGUAGES.index(code)
    choices = tuple((EXPLICIT[intent][i], EXPLICIT[intent][i]) for intent in intents)
    return 'command_choice160', {}, (('command', 'choice', 'choose', '', choices),)


@lru_cache(maxsize=7)
def _heads(code):
    from jarvis.runtime.canonical_commands import templates
    # Explicit complete defaults outrank a coincident longer template prefix.
    # For example weather without a city keeps the configured location; only
    # an actually dangling "weather in ..." asks for a city.
    default_tools = {'weather', 'screen', 'mini_browser'}
    complete_defaults = set()
    path = Path(__file__).resolve().parents[1] / 'locales' / 'commands' / (code + '.json')
    for intent, phrases in json.loads(path.read_text(encoding='utf-8')).get('action_phrases', {}).items():
        if intent.split('.', 1)[0] in default_tools:
            complete_defaults.update(norm(strip_request_shell(phrase, code)) for phrase in phrases if '{' not in phrase)
    owners = {}
    for row in templates(code):
        match = re.search(r'\{[a-z_]+\}', row.phrase)
        if match is None:
            continue
        head = norm(row.phrase[:match.start()])
        if len(head.split()) >= 2:
            owners.setdefault(head, set()).add(row.intent)
    result = {}
    for head, intents in owners.items():
        if head in complete_defaults:
            continue
        own = intents & CONTRACTS.keys()
        if not own:
            continue
        if own & {'shelf.check', 'overlay.checklist_check'} and len(intents) > 1:
            # A bare item is not proof of whether its list lives on the shelf or
            # on a floating panel. In particular it must never close an app.
            result[head] = _choice(('shelf.check', 'overlay.checklist_check'), code)
        elif {'overlay.checklist_complete_all', 'overlay.checklist_uncheck_all'} <= intents:
            result[head] = _choice(('overlay.checklist_complete_all', 'overlay.checklist_uncheck_all'), code)
        elif {'weather.sunrise', 'weather.sunset'} <= intents:
            result[head] = _choice(('weather.sunrise', 'weather.sunset'), code)
        elif 'filesystem.reveal' in own and 'filesystem.open' in intents:
            result[head] = _choice(('filesystem.open', 'filesystem.reveal'), code)
        elif 'window_advanced.focus_instance' in own and 'window.focus' in intents:
            # "switch to the window" has the ordinary title-based meaning; the
            # explicit instance forms separately request an application + number.
            result[head] = 'window', {'action': 'focus'}, (slot('title', prompt='window156'),)
        elif len(intents) == 1:
            result[head] = _contract(next(iter(own)))
    i = LANGUAGES.index(code)
    for intent, phrases in EXPLICIT.items():
        if intent in CONTRACTS:
            result[norm(phrases[i])] = _contract(intent)
    return result


def partial(text, code):
    if code not in LANGUAGES:
        return None
    value = norm(strip_request_shell(text, code))
    # Keep established typed file-open heads (e.g. German 'zeige die Datei').
    # A newly discovered template-prefix collision must not replace that
    # reviewed name resolver with an open/reveal menu. Other ambiguous view
    # heads such as Spanish 'mostrar el archivo' still ask the choice.
    from jarvis.runtime.dialogue_tasks import _heads as original_heads
    for tool, action, _field, _kind, prompt, pattern in original_heads(code):
        match = pattern.fullmatch(value)
        if tool == 'desktop' and action == 'open' and prompt == 'file' and match and not (match.group('tail') or '').strip():
            return None
    return _heads(code).get(value)
