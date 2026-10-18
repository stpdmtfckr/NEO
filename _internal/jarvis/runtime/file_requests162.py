"""Explicit local lookup shells; filenames and directory scopes stay literal.

No disk access, fuzzy title rewriting, browser fallback or command execution.
The same grammar supplies missing-target clarification and complete requests.
"""
from __future__ import annotations

from functools import lru_cache
import re

from jarvis.runtime.natural_commands import LANGUAGES, VERBS, negated_action, strip_request_shell

_OBJECTS = {
    'ru': r'(?:файл(?:ы)?|документ(?:ы)?|папк[ауи]|каталог(?:и)?)',
    'en': r'(?:(?:the|a|an|my)\s+)?(?:files?|documents?|folders?|director(?:y|ies))',
    'de': r'(?:(?:die|das|den|eine|einen|meine)\s+)?(?:Datei(?:en)?|Dokument(?:e)?|Ordner|Verzeichnis(?:se)?)',
    'fr': r'(?:(?:le|la|les|un|une|mes)\s+)?(?:fichiers?|documents?|dossiers?|répertoires?)',
    'es': r'(?:(?:el|la|los|las|un|una|mis)\s+)?(?:archivos?|ficheros?|documentos?|carpetas?|directorios?)',
    'it': r'(?:(?:il|la|i|le|un|una|miei)\s+)?(?:file|document[oi]|cartell[ae]|directory)',
    'pt': r'(?:(?:o|a|os|as|um|uma|meus)\s+)?(?:ficheiros?|arquivos?|documentos?|pastas?|diretórios?)',
}
_WHERE = {
    'ru': r'где\s+(?:(?:находит(?:ся|ься)|находятся|лежит|лежат|хранится|хранятся)\s+)?',
    'en': r'where\s+(?:is|are)\s+', 'de': r'wo\s+(?:ist|sind|liegt|liegen)\s+',
    'fr': r'où\s+(?:est|sont|se trouve|se trouvent)\s+',
    'es': r'dónde\s+(?:está|están|se encuentra|se encuentran)\s+',
    'it': r'dove\s+(?:è|sono|si trova|si trovano)\s+',
    'pt': r'onde\s+(?:está|estão|fica|ficam)\s+',
}
_CONTENT = {
    'ru': r'по содержимому|с текстом|содержащ(?:ий|ие)\s+текст',
    'en': r'containing|with (?:the )?text|by content',
    'de': r'mit (?:dem )?(?:Inhalt|Text)|nach Inhalt',
    'fr': r'contenant|avec le texte|par contenu',
    'es': r'que contenga(?:n)?|con el texto|por contenido',
    'it': r'che conteng(?:a|ono)|con il testo|per contenuto',
    'pt': r'que contenha(?:m)?|com o texto|por conteúdo',
}
_SCOPE = {
    'ru': r'(?:в|из)\s+(?:папке|папки|каталоге|каталога|директории)',
    'en': r'in\s+(?:(?:the|a)\s+)?(?:folder|directory)',
    'de': r'in\s+(?:(?:dem|der|den)\s+)?(?:Ordner|Verzeichnis)',
    'fr': r'dans\s+(?:(?:le|un)\s+)?(?:dossier|répertoire)',
    'es': r'en\s+(?:(?:la|el)\s+)?(?:carpeta|directorio)',
    'it': r'(?:nella|in una|in)\s+(?:cartella|directory)',
    'pt': r'(?:na|no|em)\s+(?:pasta|diretório)',
}
_SEARCH = {'ru': 'поиск файлов', 'en': 'file search', 'de': 'Dateisuche',
           'fr': 'recherche de fichiers', 'es': 'búsqueda de archivos',
           'it': 'ricerca file', 'pt': 'pesquisa de ficheiros'}
_DIRECT_PREP = {'ru': r'в|на|из', 'en': r'in|on', 'de': r'in|im|auf',
                'fr': r'dans|sur', 'es': r'en', 'it': r'in|su', 'pt': r'em|na|no'}
_DIRECT_FOLDERS = {
    'ru': r'загрузках|загрузки|документах|документы|рабочем столе|изображениях',
    'en': r'downloads|documents|desktop|pictures',
    'de': r'Downloads|Dokumente|Desktop|Bilder',
    'fr': r'téléchargements|documents|bureau|images',
    'es': r'descargas|documentos|escritorio|imágenes',
    'it': r'scaricati|documenti|scrivania|immagini',
    'pt': r'transferências|documentos|ambiente de trabalho|imagens',
}
_QUOTES = {'"': '"', '«': '»', '“': '”', "'": "'"}


def _unquote(value):
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and _QUOTES.get(value[0]) == value[-1] else value


def _outside_quotes(value, end):
    closing = None
    for ch in value[:end]:
        if closing:
            if ch == closing:
                closing = None
        elif ch in _QUOTES:
            closing = _QUOTES[ch]
    return closing is None


@lru_cache(maxsize=7)
def _patterns(code):
    verb = r'(?:' + VERBS[code][2] + r')'
    head = r'(?:' + verb + r'\s+|' + _WHERE[code] + r')' + _OBJECTS[code]
    extension = r'(?:pdf|docx?|xlsx?|pptx?|txt|rtf|csv|json|ya?ml|ini|cfg|log|md|png|jpe?g|gif|webp|mp[34]|wav|flac|mkv|zip|7z|rar|exe|lnk)'
    filename = r'(?:[^\s"«»]+\.' + extension + r'|"[^"\n]+\.' + extension + r'"|«[^»\n]+\.' + extension + r'»)' 
    qualifier = r'(?:' + _SCOPE[code] + r'|(?:' + _DIRECT_PREP[code] + r'))\s+.+'
    # An explicit filename needs no extra word "file". Restrict this shorthand
    # to known file extensions; domains and ordinary web queries stay untouched.
    bare = re.compile(verb + r'\s+(?P<query>' + filename + r'(?:\s+' + qualifier + r')?)', re.I)
    # Content mode must own its complete shell before generic name search.
    return (
        ('content', re.compile(head + r'\s+(?:' + _CONTENT[code].replace(' ', r'\s+') + r')(?:\s+(?P<query>.*))?', re.I)),
        ('files', re.compile(head + r'(?:\s+(?P<query>.*))?', re.I)),
        ('files', re.compile(re.escape(_SEARCH[code]) + r'(?:\s+(?P<query>.*))?', re.I)),
        ('files', bare),
    )


def parse_file_request(text, code='ru', *, allow_empty=False):
    if code not in LANGUAGES:
        return None
    value = strip_request_shell(text, code).strip().rstrip('?!').strip()
    if negated_action(value, code):
        return None
    # An explicit Explorer destination outranks generic filename lookup.
    # Do not swallow the destination into the query or reinterpret quoted data.
    if code == 'en':
        reveal = re.fullmatch(r'(?:locate|find)\s+(?:the\s+)?file\s+(?P<source>.+?)\s+(?P<destination>in\s+(?:File\s+)?Explorer)', value, re.I)
        if reveal and _outside_quotes(value, reveal.start('destination')):
            return 'filesystem', {'action': 'reveal', 'source': _unquote(reveal['source'])}
    for action, pattern in _patterns(code):
        found = pattern.fullmatch(value)
        if not found:
            continue
        query = (found.group('query') or '').strip()
        args = {'action': action}
        # A directory qualifier is recognized only outside a quoted filename.
        # Generic "in" is deliberately not a delimiter (names may contain it).
        scopes = list(re.finditer(r'(?:^|\s)' + _SCOPE[code] + r'\s+(?P<root>.+)$', query, re.I))
        for scope in scopes:
            if _outside_quotes(query, scope.start()):
                args['root'] = _unquote(scope['root'])
                query = query[:scope.start()].strip()
                break
        if 'root' not in args:
            known = _DIRECT_FOLDERS[code] + r'|Downloads|Documents|Desktop|Pictures'
            direct = re.search(r'(?:^|\s)(?:' + _DIRECT_PREP[code] + r')\s+(?P<root>(?:' + known + r')|(?:[A-Za-z]:[\\/]|/|~[\\/]).+)$', query, re.I)
            if direct and _outside_quotes(query, direct.start()):
                args['root'] = direct['root']
                query = query[:direct.start()].strip()
        if action == 'files' and len(query) >= 2 and _QUOTES.get(query[0]) == query[-1]:
            args['literal'] = True
        query = _unquote(query)
        if query:
            args['query'] = query
            return 'search', args
        if allow_empty:
            return 'search', args
        return None
    return None


def partial_file_request(text, code):
    result = parse_file_request(text, code, allow_empty=True)
    if result is None or result[0] != 'search' or result[1].get('query'):
        return None
    tool, args = result
    prompt = 'file_content162' if args['action'] == 'content' else 'file_lookup162'
    return tool, args, (('query', 'text' if args['action'] == 'content' else 'name', prompt),)
