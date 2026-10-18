"""Missing targets that need domain identity, never a guessed default object."""
from __future__ import annotations
import re
from jarvis.runtime.natural_commands import LANGUAGES, VERBS, OBJECTS, _ARTICLES, norm, strip_request_shell

LAUNCH = (
 r'запусти|запустить|запускай|открой|открыть|включи',
 r'launch|start|run|open', r'starte|starten|öffne|oeffne|öffnen',
 r'lance|lancer|ouvre|ouvrir', r'lanza|lanzar|inicia|iniciar|abre|abrir',
 r'avvia|avviare|apri|aprire', r'inicia|inicie|iniciar|abre|abra|abrir',
)
GAME_QUESTIONS = (
 'Какую игру запустить? Назови её название.', 'Which game should I launch? Tell me its name.',
 'Welches Spiel soll ich starten? Nenne den Namen.', 'Quel jeu dois-je lancer ? Donne son nom.',
 '¿Qué juego inicio? Dime su nombre.', 'Quale gioco avvio? Dimmi il nome.',
 'Que jogo devo iniciar? Diz o nome.',
)
LOCATION_QUESTIONS = (
 'Файлы какой игры или программы найти? Назови её название.',
 'Which game or application should I find files for? Tell me its name.',
 'Für welches Spiel oder Programm soll ich Dateien finden? Nenne den Namen.',
 'Pour quel jeu ou programme dois-je trouver les fichiers ? Donne son nom.',
 '¿De qué juego o programa busco los archivos? Dime su nombre.',
 'Di quale gioco o programma cerco i file? Dimmi il nome.',
 'De que jogo ou programa devo procurar os ficheiros? Diz o nome.',
)


def partial(text, code):
    if code not in LANGUAGES:
        return None
    from jarvis.runtime.clarification_contracts157 import slot
    value = norm(strip_request_shell(text, code)); i = LANGUAGES.index(code)
    articles = r'(?:' + _ARTICLES[code] + r'\s+)?'
    for index, kind in ((3,'game'), (0,'app')):
        pattern = '(?:' + LAUNCH[i] + r')\s+' + articles + '(?:' + OBJECTS[code][index] + ')'
        if re.fullmatch(pattern, value, re.I):
            return 'application', {'action':'open', **({'kind':'game'} if kind=='game' else {})}, (slot('name','name','game160' if kind=='game' else 'application'),)
    # The complete location grammar remains the single owner. Its exact prefix
    # without a title needs the same typed name; no drive walk is started yet.
    from jarvis.runtime.location_requests158 import _PATTERNS, missing_location_request
    if missing_location_request(value, code):
        return 'game_locations', {'action':'find','kind':'all'}, (slot('name','name','location160'),)
    for pattern in _PATTERNS.get(code, ()):
        if not pattern.endswith('(?P<name>.+)'):
            continue
        head = pattern[:-len('(?P<name>.+)')].rstrip()
        if re.fullmatch(head, value, re.I):
            return 'game_locations', {'action':'find','kind':'all'}, (slot('name','name','location160'),)
    return None


def complete(text, code):
    """Fallback for an explicit typed target, copying the title byte for byte.

    Called only after native complete grammar. No generic noun/default is used.
    """
    if code not in LANGUAGES:
        return None
    value=strip_request_shell(text,code).strip();i=LANGUAGES.index(code)
    articles=r'(?:'+_ARTICLES[code]+r'\s+)?'
    for index,kind in ((3,'game'),(0,'app')):
        pattern='(?:'+LAUNCH[i]+r')\s+'+articles+'(?:'+OBJECTS[code][index]+r')\s+(?P<name>.+)'
        match=re.fullmatch(pattern,value,re.I)
        if not match:
            continue
        name=match['name'].strip()
        if len(name)>=2 and (name[0],name[-1]) in {('"','"'),('«','»'),('“','”')}:
            name=name[1:-1]
        elif re.search(r'\s(?:и|and|und|et|y|e)\s|[;\n\r]',name,re.I) or _SEQUENCE182.search(name):
            return None             # NEO 182: «… затем/then/dann/puis/luego/poi/depois …» is a plan of several actions
        else:
            name=name.rstrip(' ?!.…¿¡')   # NEO 182: «Could you open application Calculator?» → Calculator
        if not name or len(name)>1000:
            return None
        return 'application',{'action':'open','name':name,**({'kind':'game'} if kind=='game' else {})}
    return None


_SEQUENCE182=re.compile(r'(?:,\s*|\s)(?:затем|потом|после этого|then|after that|afterwards|next|dann|danach|anschließend|puis|ensuite|après ça|luego|después|despues|poi|dopo|quindi|depois|em seguida|a seguir)\s',re.I)


def bare_browser(text,code):
    if code not in LANGUAGES:return None
    names=('браузер','browser','Browser','navigateur','navegador','browser','navegador')
    i=LANGUAGES.index(code)
    pattern='(?:'+LAUNCH[i]+r')\s+(?:'+_ARTICLES[code]+r'\s+)?'+names[i]
    if re.fullmatch(pattern,norm(strip_request_shell(text,code)),re.I):
        return 'application',{'action':'open','name':'browser','kind':'app'}
    return None
