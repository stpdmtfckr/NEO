"""NEO 178: Word, Excel and PowerPoint — documents (.docx), spreadsheets (.xlsx), presentations (.pptx).

What NEO can do, by voice or text, in seven languages:

* create a document / spreadsheet / presentation — empty, with a title, with text, with columns,
  with slides; from a topic the local NEO model writes the text or the slide outline;
* read one aloud or summarize it (spreadsheets: sheets and first rows; presentations: slides);
* add a paragraph or a heading, replace words; write or read a cell, add a row or a sheet,
  sum / average / min / max of a column; add a slide with points;
* save, export to PDF (through Office or LibreOffice when installed) or a sheet to CSV;
* undo NEO's last change of a file.

Which document: a named file («в файле отчёт.xlsx»), else the document open in Word / Excel /
PowerPoint (changed live through Office itself — Ctrl+Z works there), else the document NEO made
or changed last, else the file selected in File Explorer.

Files are changed only on the user's command; before a file on disk is changed a copy is kept
for «отмени изменения». Document text is never written to the logs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
KINDS = {'docx': '.docx', 'xlsx': '.xlsx', 'pptx': '.pptx'}
SUFFIX_KIND = {'.docx': 'docx', '.docm': 'docx', '.xlsx': 'xlsx', '.xlsm': 'xlsx', '.pptx': 'pptx', '.pptm': 'pptx'}
_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {'last': {}, 'created': set()}      # kind -> (path, time); paths NEO created


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _n(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').casefold().replace('ё', 'е')).strip(' .,!?:;¿¡«»"\'')


# ---- phrases ------------------------------------------------------------------------------------

_CREATE = r'(?:создай|сделай|создать|открой\s+нов\w+|заведи|create|make|start|new|erstelle|erzeuge|mach|lege\s+an|cr[eé]e|cr[eé]er|fais|g[eé]n[eè]re|crea|haz|genera|cria|crie|faz|fa[çc]a|gere)'
_NEW = r'(?:(?:новый|новую|новое|a\s+new|a|an|new|ein\w*\s+neue\w*|ein\w*|neue\w*|un\s+nouveau|une\s+nouvelle|un|une|nouveau|nouvelle|un\s+nuevo|una\s+nueva|un|una|nuevo|nueva|un\s+nuovo|una\s+nuova|nuovo|nuova|um\s+novo|uma\s+nova|um|uma|novo|nova)\s+)*'
_DOC = (r'(?:(?<!текстовый\s)(?<!текстовой\s)документ\w*(?:\s+(?:word|ворд\w*))?|(?:word|ворд)(?:-?\s*(?:документ\w*|файл\w*|document|file|dokument|datei))?|(?:word\s+)?document|word\s+file|'
        r'(?:word-?)?dokument|textdokument|document(?:\s+word)?|fichier\s+word|texte\s+word|documento(?:\s+(?:de\s+)?word)?|archivo\s+de\s+word|file\s+word|ficheiro\s+word)')
_SHEET = (r'(?:таблиц\w*(?:\s+(?:excel|эксель))?|(?:excel|эксель)(?:-?\s*(?:файл\w*|таблиц\w*|книг\w*|file|workbook|sheet|table|datei|tabelle|arbeitsmappe))?|книг\w+\s+excel|электронн\w+\s+таблиц\w*|spreadsheet|'
          r'excel\s+(?:file|workbook|sheet|table)|workbook|(?:excel-?)?tabelle|excel-?datei|arbeitsmappe|classeur(?:\s+excel)?|tableur|tableau(?:\s+excel)?|feuille\s+de\s+calcul|fichier\s+excel|'
          r'hoja\s+de\s+c[aá]lculo|hoja\s+de\s+excel|tabla(?:\s+de\s+excel)?|libro\s+de\s+excel|archivo\s+de\s+excel|tabella(?:\s+excel)?|foglio\s+(?:di\s+calcolo|excel)|file\s+excel|cartella\s+di\s+lavoro|'
          r'folha\s+de\s+c[aá]lculo|planilha(?:\s+(?:do\s+)?excel)?|tabela(?:\s+(?:do\s+)?excel)?|livro\s+(?:do\s+)?excel|ficheiro\s+excel)')
_DECK = (r'(?:презентаци\w*(?:\s+(?:powerpoint|пауэрпоинт))?|(?:powerpoint|пауэрпоинт)(?:\s+(?:файл\w*|презентаци\w*))?|слайды|presentation|slides?|slide\s+deck|deck|'
         r'pr[aä]sentation|powerpoint-?(?:datei|pr[aä]sentation)|pr[eé]sentation|diaporama|presentaci[oó]n|presentazione|apresenta[cç][aã]o)')
_TITLED = (r'(?:с\s+названием|под\s+названием|названи\w+|с\s+заголовком|called|named|titled|with\s+the\s+(?:title|name)|namens|mit\s+dem\s+(?:titel|namen)|'
           r'nomm[eé]e?|intitul[eé]e?|appel[eé]e?|llamad[oa]|titulad[oa]|con\s+el\s+(?:t[ií]tulo|nombre)|chiamat[oa]|intitolat[oa]|col\s+titolo|'
           r'chamad[oa]|intitulad[oa]|com\s+o\s+(?:t[ií]tulo|nome))')
_ABOUT = r'(?:про|о|об|обо|на\s+тему|по\s+теме|about|on|on\s+the\s+topic\s+of|über|zum\s+thema|sur|au\s+sujet\s+de|sobre|acerca\s+de|su|sul|sulla|sullo|sui|sugli|sulle|riguardo|a\s+proposito\s+di)'
_WITH_TEXT = r'(?:с\s+текстом|текст(?:ом)?|with\s+(?:the\s+)?text|containing|mit\s+(?:dem\s+)?text|avec\s+(?:le\s+)?texte|con\s+(?:el\s+)?texto|con\s+(?:il\s+)?testo|com\s+(?:o\s+)?texto)'
_COLUMNS = (r'(?:с\s+(?:колонками|столбцами)|колонки|столбцы|with\s+(?:the\s+)?columns|columns|mit\s+(?:den\s+)?spalten|spalten|avec\s+(?:les\s+)?colonnes|colonnes|'
            r'con\s+(?:las\s+)?columnas|columnas|con\s+(?:le\s+)?colonne|colonne|com\s+(?:as\s+)?colunas|colunas)')
_SLIDES_N = (r'(?:на|из|with|mit|avec|de|con|di|com)\s+(?P<n>\d{1,2})\s+(?:слайд\w*|slides?|folien?|diapositives?|diapositivas?|diapositive|diapositivos?)')

_CELL = r'(?P<cell>[a-zA-Z]{1,3}\s?\d{1,6})'
_CELL_WORD = r'(?:(?:в\s+)?(?:ячейк\w*|клетк\w*)|(?:in\s+)?(?:the\s+)?cell|(?:in\s+(?:die\s+)?)?zelle|(?:dans\s+)?(?:la\s+)?cellule|(?:en\s+)?(?:la\s+)?celda|(?:nella\s+|in\s+)?cella|(?:na\s+)?c[eé]lula)'
_WRITE = r'(?:напиши|запиши|поставь|введи|впиши|вставь|write|put|enter|type|set|schreibe?|trage|setze|[eé]cris|[eé]crire|mets|saisis|escribe|pon|introduce|scrivi|metti|inserisci|escreve|escreva|p[oõ]e|ponha|insere|insira|coloca|coloque|digite)'
_STATS = {
    'sum': r'(?:сумм\w*|посчитай|итог\w*|sum|total|summe|addiere|somme|additionne|suma|suma\s+total|somma|soma)',
    'average': r'(?:средн\w*|average|mean|durchschnitt|mittelwert|moyenne|promedio|media|m[eé]dia)',
    'max': r'(?:максимум\w*|максимальн\w+|наибольш\w+|max(?:imum)?|h[oö]chst\w*|m[aá]ximo|massimo)',
    'min': r'(?:минимум\w*|минимальн\w+|наименьш\w+|min(?:imum)?|kleinst\w*|m[ií]nimo|minimo)',
}
_COLUMN = r'(?:(?:столбц\w*|колонк\w*|column|spalte|colonne|columna|colonna|coluna)\s+(?P<col>[a-zA-Z]{1,3})\b|(?P<col2>[a-zA-Z]{1,3})\s*:\s*(?P=col2))'
_ROW = r'(?:строк\w*|ряд|row|zeile|ligne|fila|riga|linha)'
_SHEET_WORD = r'(?:лист\w*|sheet|worksheet|blatt|tabellenblatt|feuille|hoja|foglio|folha)'
_SLIDE = r'(?:слайд\w*|slide|folie|diapositive|diapositiva|diapositivo)'
_ADD = r'(?:добавь|допиши|вставь|add|append|insert|f[uü]ge|h[aä]nge|ajoute|ins[eè]re|a[nñ]ade|agrega|inserta|aggiungi|inserisci|adiciona|acrescenta|insere)'
_PARAGRAPH = r'(?:абзац\w*|параграф\w*|текст\w*|строчк\w*|paragraph|text|absatz|paragraphe|texte|p[aá]rrafo|texto|paragrafo|testo|par[aá]grafo)'
_HEADING = r'(?:заголов\w*|heading|title|[uü]berschrift|titre|t[ií]tulo|titolo)'
_INTO_DOC = (r'(?:\s+(?:в|к)\s+(?:этот\s+|текущий\s+)?(?:документ\w*|ворд\w*|word)|\s+(?:to|into)\s+(?:the|this)\s+document|\s+(?:zum|ins|in\s+das)\s+dokument|'
             r'\s+(?:au|dans\s+le)\s+document|\s+al\s+documento|\s+(?:al|nel)\s+documento|\s+ao\s+documento)?')
_REPLACE = (r'(?:замени|поменяй|replace|change|ersetze|[aä]ndere|remplace|reemplaza|sustituye|cambia|sostituisci|substitui|troca)\s+(?P<a>.+?)\s+'
            r'(?:на|with|by|to|durch|mit|par|por|con|com|em)\s+(?P<b>.+?)')
_SAVE = r'(?:сохрани|save|speichere|enregistre|sauvegarde|guarda|guarde|salva|salve|grava|grave)'
_THIS_DOC = (r'(?:(?:этот|эту|это|текущ\w+|открыт\w+)\s+)?(?:документ\w*|таблиц\w*|презентаци\w*|файл\w*|книг\w*)|(?:the|this)\s+(?:document|spreadsheet|presentation|file|workbook)|'
             r'(?:das|dieses|die|diese)\s+(?:dokument|tabelle|pr[aä]sentation|datei)|(?:le|ce|la|cette)\s+(?:document|classeur|pr[eé]sentation|fichier)|'
             r'(?:el|este|la|esta)\s+(?:documento|hoja|presentaci[oó]n|archivo)|(?:il|questo|la|questa)\s+(?:documento|foglio|presentazione|file)|'
             r'(?:o|este|a|esta)\s+(?:documento|folha|planilha|apresenta[cç][aã]o|ficheiro)')
_PDF = r'(?:в\s+(?:формат\w*\s+)?|как\s+|as\s+(?:a\s+)?|to\s+|into\s+|als\s+|in\s+|nach\s+|en\s+|comme\s+|vers\s+|como\s+|a\s+|come\s+|para\s+|em\s+)?pdf'
_CSV = r'(?:в\s+(?:формат\w*\s+)?|как\s+|as\s+(?:a\s+)?|to\s+|into\s+|als\s+|in\s+|nach\s+|en\s+|comme\s+|vers\s+|como\s+|a\s+|come\s+|para\s+|em\s+)?csv'
_UNDO = (r'(?:(?:отмени|верни|откати)\s+(?:последн\w+\s+)?(?:изменени\w*|правк\w*)(?:\s+в\s+(?:документе|таблице|презентации|файле))?|верни\s+(?:документ|таблицу|презентацию|файл)\s+как\s+было|'
         r'undo\s+(?:the\s+|my\s+|your\s+)?(?:last\s+)?(?:document\s+|spreadsheet\s+|presentation\s+|file\s+)?(?:change|edit)s?|'
         r'(?:mach|nimm)\s+(?:die\s+)?(?:letzte\s+)?\w*[aä]nderung\w*\s+r[uü]ckg[aä]ngig|annule\s+(?:la\s+)?(?:derni[eè]re\s+)?modification|'
         r'(?:deshaz|deshacer)\s+(?:el\s+|los\s+)?(?:[uú]ltimo\s+)?cambios?|annulla\s+(?:l[\'’]ultima\s+)?modific\w+|(?:desfaz|desfa[çc]a)\s+(?:a\s+)?(?:[uú]ltima\s+)?altera[cç][aã]o)')
# «… в документе / to the document / du document …» after an undo request
_DOC_TAIL = (r'\s+(?:в|во|к|to|in|of|im|in\s+der|des|du|dans\s+le|de\s+la|del|de\s+la|en\s+el|nel|della|do|da|no|na)\s+'
             r'(?:the\s+|this\s+|dem\s+|der\s+|le\s+|la\s+|el\s+|il\s+|o\s+|a\s+)?(?:документ\w*|таблиц\w*|презентаци\w*|файл\w*|word|excel|powerpoint|'
             r'document|spreadsheet|presentation|file|dokument|datei|tabelle|pr[aä]sentation|fichier|classeur|pr[eé]sentation|documento|archivo|hoja|presentaci[oó]n|'
             r'foglio|presentazione|ficheiro|planilha|folha|apresenta[cç][aã]o)$')
# Words that put a Word/Excel/PowerPoint document in the request (without them a bare «замени X на Y» stays with the text-file tools)
_OFFICE_CONTEXT = r'(?:документ|таблиц|презентаци|слайд|ворд|эксел|эксель|пауэрпоинт|word|excel|powerpoint|document|spreadsheet|presentation|dokument|tabelle|pr[aä]sentation|classeur|pr[eé]sentation|documento|hoja|presentaci|foglio|presentazione|planilha|folha|apresenta)'
_READ = (r'(?:прочитай|прочти|зачитай|покажи|что\s+(?:в|написано\s+в)|read(?:\s+(?:me|out))?|what(?:\W?s|\s+is)\s+in|show\s+me|lies(?:\s+mir)?|was\s+steht\s+in|'
         r'zeig\s+mir|lis|lis-moi|qu[\'’]y\s+a-t-il\s+dans|montre-moi|lee(?:me)?|qu[eé]\s+hay\s+en|mu[eé]strame|leggi(?:mi)?|cosa\s+c[\'’]?[eè]\s+(?:in|nel(?:la)?)|mostrami|'
         r'l[eê](?:-me)?|o\s+que\s+(?:est[aá]|h[aá])\s+n[oa]|mostra-me)')
_SUMMARIZE = r'(?:перескажи|кратко\s+перескажи|сделай\s+выжимку|summari[sz]e|fasse\s+zusammen|r[eé]sume|resume|resumir|riassumi|resume)'
_FILE_NAME = r'(?P<file>[\w\s\-().,]{1,80}?\.(?:docx|docm|xlsx|xlsm|pptx|pptm|doc|xls|ppt))'
_IN_FILE = rf'(?:\s+(?:в\s+файле|в|in\s+(?:the\s+file\s+)?|im\s+|in\s+der\s+datei\s+|dans\s+(?:le\s+fichier\s+)?|en\s+(?:el\s+archivo\s+)?|nel\s+file\s+|no\s+ficheiro\s+)\s*{_FILE_NAME})?'


_EXT = r'(?:docx|docm|xlsx|xlsm|pptx|pptm|doc|xls|ppt)'


def _file_name(raw: str) -> str:
    """«отчёт.xlsx», «в файле Годовой отчёт.docx», «"План 2025.pptx"» -> the file name the user said."""
    quoted = re.search(rf'[«"“]([^«»"“”]+?\.{_EXT})[»"”]', raw, re.I)
    if quoted:
        return quoted.group(1).strip()
    marked = re.search(rf'(?:файл[еау]?|file|datei|fichier|archivo|ficheiro)\s+(?P<f>[^\s«»"].{{0,60}}?\.{_EXT})\b', raw, re.I)
    if marked:
        return marked.group('f').strip()
    bare = re.search(rf'(?P<f>[^\s«»"]+\.{_EXT})\b', raw, re.I)
    return bare.group('f') if bare else ''


def _kind_of(text: str) -> str:
    if re.search(_DECK + '|' + _SLIDE, text):
        return 'pptx'
    if re.search(_SHEET + '|' + _CELL_WORD + '|' + _SHEET_WORD + r'\b|\b(?:excel|эксель)', text):
        return 'xlsx'
    if re.search(_DOC, text):
        return 'docx'
    return ''


def _clean(value: str) -> str:
    return str(value or '').strip(' "«»“”\'.,:;')


def _raw_tail(raw: str, start_word_count: int) -> str:
    """The user's own spelling (case, punctuation) of the text after the first words."""
    parts = raw.split()
    return ' '.join(parts[start_word_count:])


def _original(raw: str, lowered_piece: str) -> str:
    """The piece as the user typed it (the parser works on lowered text)."""
    if not lowered_piece:
        return ''
    folded = raw.casefold().replace('ё', 'е')
    at = folded.find(lowered_piece)
    return raw[at:at + len(lowered_piece)] if at >= 0 else lowered_piece


def parse(text: str, code: str = 'ru') -> tuple[str, dict[str, Any]] | None:
    """('office', arguments) for a request about a document, spreadsheet or presentation, or None."""
    raw = ' '.join(str(text or '').split()).strip()
    t = re.sub(r'^(?:(?:нео|neo|пожалуйста|please|bitte|por\s+favor|per\s+favore|s[\'’]il\s+te\s+pla[iî]t)[\s,]+)+', '', _n(raw))
    if not t or len(t) > 600:
        return None
    # German puts the verb last: «als pdf speichern», «eine tabelle erstellen», «die änderung rückgängig machen»
    t = re.sub(r'^(?P<o>.+?)\s+(?:erstellen|anlegen)$', r'erstelle \g<o>', t)
    t = re.sub(r'^(?P<o>.+?)\s+speichern$', r'speichere \g<o>', t)
    t = re.sub(r'^(?P<o>.+?)\s+exportieren$', r'exportiere \g<o>', t)
    t = re.sub(r'^(?P<o>.+?)\s+r[uü]ckg[aä]ngig\s+machen$', r'mach \g<o> rückgängig', t)
    file_name = _file_name(raw)
    named = {'name': file_name} if file_name else {}
    context = bool(named) or bool(re.search(_OFFICE_CONTEXT, t))

    # Create: «создай документ "План" с текстом …», «создай таблицу с колонками имя, телефон», «создай презентацию про космос на 5 слайдов»
    create = re.match(rf'{_CREATE}\s+{_NEW}(?P<what>{_DOC}|{_SHEET}|{_DECK})\b(?P<rest>.*)$', t)
    if create:
        kind = 'pptx' if re.fullmatch(_DECK, create.group('what')) else 'xlsx' if re.fullmatch(_SHEET, create.group('what')) else 'docx'
        rest = create.group('rest').strip(' ,')
        args: dict[str, Any] = {'action': 'create', 'kind': kind}
        slides = re.search(_SLIDES_N, rest)
        if slides:
            args['count'] = int(slides.group('n'))
            rest = (rest[:slides.start()] + rest[slides.end():]).strip(' ,')
        columns = re.search(rf'{_COLUMNS}\s*:?\s*(?P<v>.+)$', rest)
        if columns and kind == 'xlsx':
            args['values'] = _original(raw, columns.group('v')).strip(' .')
            rest = rest[:columns.start()].strip(' ,')
        body = re.search(rf'{_WITH_TEXT}\s*:?\s*(?P<v>.+)$', rest)
        if body:
            args['text'] = _clean(_original(raw, body.group('v')))
            rest = rest[:body.start()].strip(' ,')
        titled = re.match(rf'{_TITLED}\s*:?\s*(?P<v>.+)$', rest)
        topic = re.match(rf'{_ABOUT}\s+(?P<v>.+)$', rest)
        if titled:
            args['title'] = _clean(_original(raw, titled.group('v')))
        elif topic:
            args['topic'] = _clean(_original(raw, topic.group('v')))
        elif rest:
            quoted = re.search(r'[«"“](.+?)[»"”]', raw)
            if quoted:
                args['title'] = quoted.group(1).strip()
            elif len(rest.split()) <= 6:
                title = _original(raw, rest)
                # An unquoted implicit title can have a command courtesy
                # suffix. Do not strip anything from quoted titles or bodies.
                courtesy = {'ru': r'пожалуйста', 'en': r'please', 'de': r'bitte',
                            'fr': r"s['’]il\s+(?:te|vous)\s+pla[iî]t", 'es': r'por\s+favor',
                            'it': r'per\s+favore', 'pt': r'por\s+favor'}[_code(code)]
                title = re.sub(r'[,;]\s*(?:' + courtesy + r')[.!?]*$', '', title, flags=re.I)
                if title.strip():
                    args['title'] = _clean(title)
            else:
                return None
        return 'office', args

    # Undo
    undo_tail = re.search(_DOC_TAIL, t)
    if re.fullmatch(_UNDO, t[:undo_tail.start()] if undo_tail else t):
        # Without «в документе / таблице» it is NEO's own last Office change only if there is one (see claims()).
        return 'office', {'action': 'undo', **({} if context else {'soft': True})}

    # Excel: cells, rows, sheets, column statistics
    write = (re.fullmatch(rf'(?:{_CELL_WORD}\s+)?{_CELL}\s*(?:{_WRITE}|=|:)\s*(?P<v>.+?){_IN_FILE}', t)
             or re.fullmatch(rf'{_WRITE}\s+(?P<v>.+?)\s+(?:в|in|into|dans|en|nella|na|in\s+die)\s+(?:{_CELL_WORD}\s+)?{_CELL}{_IN_FILE}', t)
             or re.fullmatch(rf'(?:set|setze|imposta|define)\s+(?:{_CELL_WORD}\s+)?{_CELL}\s+(?:to|auf|a|para|=)\s+(?P<v>.+?){_IN_FILE}', t)
             or re.fullmatch(rf'{_WRITE}\s+(?:{_CELL_WORD}\s+|(?:в|in|en|na|nella)\s+){_CELL}\s*(?::|=)?\s+(?P<v>.+?){_IN_FILE}', t))
    if write and not re.search(_CELL_WORD, t) and not context and not re.fullmatch(r'[=+\-]?[\d.,\s%€$₽]+|=.+', write.group('v')):
        write = None   # «напиши письмо в A4»: a cell needs the word «ячейка», a table, or a number/formula
    if write:
        value = _clean(_original(raw, write.group('v')))
        return 'office', {'action': 'set_cell', 'cell': write.group('cell').replace(' ', '').upper(), 'value': value, **named}
    read_cell = re.fullmatch(rf'(?:что\s+(?:в|записано\s+в|написано\s+в)|what(?:\W?s|\s+is)\s+in|was\s+steht\s+in|qu[\'’]y\s+a-t-il\s+dans|qu[eé]\s+hay\s+en|'
                             rf'cosa\s+c[\'’]?[eè]\s+(?:in|nella)|o\s+que\s+(?:est[aá]|h[aá])\s+na|прочитай|read)\s+(?:{_CELL_WORD}\s+)?{_CELL}{_IN_FILE}', t)
    if read_cell:
        return 'office', {'action': 'get_cell', 'cell': read_cell.group('cell').replace(' ', '').upper(), **named}
    for stat, words in _STATS.items():
        match = re.search(rf'{words}', t)
        column = re.search(_COLUMN, t)
        if match and column and re.match(rf'(?:(?:посчитай|calculate|compute|berechne|calcule|calcula|calcola)\s+)?(?:(?:the|die|la|le|el|il|a|o)\s+)?{words}', t):
            col = (column.group('col') or column.group('col2')).upper()
            return 'office', {'action': 'column_stat', 'stat': stat, 'column': col, **named}
    add_row = re.fullmatch(rf'{_ADD}\s+(?:(?:в\s+таблицу\s+)?(?:новую\s+|a\s+(?:new\s+)?|eine\s+(?:neue\s+)?|une\s+(?:nouvelle\s+)?|una\s+(?:nueva\s+|nuova\s+)?|uma\s+(?:nova\s+)?)?{_ROW})\s*(?:hinzu)?\s*:?\s*(?P<v>.+)', t)
    if add_row and not re.match(r'(?:в|во|к|to|in|into|zu|à|dans|a|en|na|no)\s+(?!таблиц|spreadsheet|sheet|tabelle|classeur|tableau|hoja|tabla|foglio|tabella|planilha|folha|tabela)', add_row.group('v')):
        return 'office', {'action': 'add_row', 'values': _original(raw, add_row.group('v')).strip(' .'), **named, **({} if context else {'soft': True})}
    add_sheet = re.fullmatch(rf'{_ADD}\s+(?:нов\w+\s+|a\s+(?:new\s+)?|ein\s+(?:neues\s+)?|une\s+(?:nouvelle\s+)?|una\s+(?:nueva\s+)?|un\s+(?:nuovo\s+)?|uma\s+(?:nova\s+)?)?{_SHEET_WORD}(?:\s+hinzu)?(?:\s+(?:{_TITLED}\s+)?(?P<v>.+))?', t)
    if add_sheet:
        return 'office', {'action': 'add_sheet', 'title': _clean(_original(raw, add_sheet.group('v') or '')), **named}

    # PowerPoint: «добавь слайд "Итоги": рост 20%; новые клиенты»
    add_slide = re.fullmatch(rf'{_ADD}\s+(?:нов\w+\s+|a\s+(?:new\s+)?|eine\s+(?:neue\s+)?|une\s+(?:nouvelle\s+)?|una\s+(?:nueva\s+|nuova\s+)?|um\s+(?:novo\s+)?)?{_SLIDE}(?:\s+hinzu)?'
                             rf'(?:\s+(?:{_TITLED}|про|о|about|über|sur|sobre|su)?\s*(?P<title>[^:]+?))?(?:\s*:\s*(?P<items>.+))?', t)
    if add_slide:
        return 'office', {'action': 'add_slide', 'title': _clean(_original(raw, add_slide.group('title') or '')),
                          'items': _original(raw, add_slide.group('items') or '').strip(), **named}

    # Word: headings, paragraphs, replace
    heading = re.fullmatch(rf'{_ADD}\s+(?:a\s+|einen\s+|un\s+|una\s+|um\s+)?{_HEADING}{_INTO_DOC}\s*(?:hinzu)?\s*:?\s*(?P<v>.+)', t)
    if heading and not re.match(r'(?:к|в|to|zu|à|a|al|ao)\s+(?:замет|чек|спис|note|list|notiz|liste)', heading.group('v')):
        return 'office', {'action': 'heading', 'text': _clean(_original(raw, heading.group('v'))), **named, **({} if context else {'soft': True})}
    para = re.fullmatch(rf'(?:{_ADD}\s+(?:(?:в|к)\s+(?:этот\s+|текущий\s+)?(?:документ\w*|ворд\w*|word)\s+|to\s+the\s+document\s+)?(?:a\s+|einen\s+|un\s+|una\s+|um\s+)?(?:{_PARAGRAPH}){_INTO_DOC}|допиши(?:\s+в\s+документ)?)\s*(?:hinzu)?\s*:\s*(?P<v>.+)', t)
    if para:
        return 'office', {'action': 'append', 'text': _clean(_original(raw, para.group('v'))), **named, **({} if context else {'soft': True})}
    replace = re.fullmatch(rf'{_REPLACE}(?:\s+(?:в|in|im|dans|en|nel|no)\s+(?:{_THIS_DOC}))?{_IN_FILE}', t)
    if replace and (re.search(_THIS_DOC, t) or named or len(replace.group('a').split()) <= 6):
        if re.search(r'(?:обои|тем[уа]|wallpaper|theme)', replace.group('a')):
            return None
        if re.search(r'(?:язык|голос|модел|громкост|яркост|назван|имя|language|voice|model|volume|brightness|name|sprache|stimme|langue|voix|idioma|voz|lingua)', replace.group('a')):
            return None
        return 'office', {'action': 'replace', 'find': _clean(_original(raw, replace.group('a'))),
                          'text': _clean(_original(raw, replace.group('b'))), **named, **({} if context else {'soft': True})}

    # Save / export
    export = re.fullmatch(rf'(?:{_SAVE}|экспортируй|export(?:iere)?|exporte|esporta|exporta)\s+(?:(?:{_THIS_DOC})\s+)?(?P<fmt>{_PDF}|{_CSV}){_IN_FILE}', t)
    if export:
        return 'office', {'action': 'export', 'format': 'csv' if 'csv' in export.group('fmt') else 'pdf', 'kind': _kind_of(t), **named}
    if re.fullmatch(rf'{_SAVE}\s+(?:{_THIS_DOC}){_IN_FILE}', t) and (named or _kind_of(t)):
        # «сохрани файл / документ» stays Ctrl+S in the active window; Office saves only a named or open Office document
        return 'office', {'action': 'save', 'kind': _kind_of(t), **named, 'soft': True}

    # Read / summarize a spreadsheet or presentation (documents on screen: explain172)
    summary = re.fullmatch(rf'{_SUMMARIZE}\s+(?P<what>{_THIS_DOC}|{_DECK}|{_SHEET}|{_DOC}){_IN_FILE}', t)
    if summary and (named or re.search(_DECK + '|' + _SHEET, summary.group('what')) or re.search(r'файл|file|datei|fichier|archivo|ficheiro', t)):
        return 'office', {'action': 'summarize', 'kind': _kind_of(t), **named}
    read = re.fullmatch(rf'{_READ}\s+(?P<what>{_THIS_DOC}|{_DECK}|{_SHEET}|{_DOC}|{_SHEET_WORD}\s+\S+){_IN_FILE}', t)
    if read and (named or re.search(_DECK + '|' + _SHEET + '|' + _SHEET_WORD, read.group('what')) or re.search(r'файл|file|datei|fichier|archivo|ficheiro', t)):
        return 'office', {'action': 'read', 'kind': _kind_of(t), **named}
    if named and re.match(r'(?:открой|open|[oö]ffne|ouvre|abre|apri)\b', t):
        return 'office', {'action': 'open', **named}
    return None


# ---- replies ------------------------------------------------------------------------------------------

_TEXT: dict[str, tuple[str, ...]] = {
    'created': ('Создал {kind} «{name}» в «Документах» и открыл его.', 'Created the {kind} “{name}” in Documents and opened it.',
                '{kind} „{name}“ in Dokumente erstellt und geöffnet.', '{kind} « {name} » créé dans Documents et ouvert.',
                'Creé {kind} «{name}» en Documentos y lo abrí.', 'Creato {kind} «{name}» in Documenti e aperto.', 'Criei {kind} «{name}» em Documentos e abri-o.'),
    'created_closed': ('Создал {kind} «{name}» в «Документах».', 'Created the {kind} “{name}” in Documents.', '{kind} „{name}“ in Dokumente erstellt.',
                       '{kind} « {name} » créé dans Documents.', 'Creé {kind} «{name}» en Documentos.', 'Creato {kind} «{name}» in Documenti.',
                       'Criei {kind} «{name}» em Documentos.'),
    'kind_docx': ('документ', 'document', 'Dokument', 'le document', 'el documento', 'il documento', 'o documento'),
    'kind_xlsx': ('таблицу', 'spreadsheet', 'Tabelle', 'le classeur', 'la hoja de cálculo', 'il foglio di calcolo', 'a folha de cálculo'),
    'kind_pptx': ('презентацию', 'presentation', 'Präsentation', 'la présentation', 'la presentación', 'la presentazione', 'a apresentação'),
    'outline_note': (' Текст написала локальная модель NEO — проверьте его.', ' The text was written by NEO’s local model — please check it.',
                     ' Den Text hat NEOs lokales Modell geschrieben – bitte prüfen.', ' Le texte a été écrit par le modèle local de NEO — vérifiez-le.',
                     ' El texto lo escribió el modelo local de NEO: revísalo.', ' Il testo è stato scritto dal modello locale di NEO: controllalo.',
                     ' O texto foi escrito pelo modelo local do NEO — verifica-o.'),
    'no_model_outline': (' Модель NEO сейчас недоступна, поэтому текст не написан: добавьте его сами или фразой «добавь слайд …».',
                         ' NEO’s model is unavailable, so no text was written: add it yourself or say “add a slide …”.',
                         ' NEOs Modell ist nicht verfügbar, daher kein Text: füge ihn selbst hinzu.', ' Le modèle de NEO est indisponible : ajoutez le texte vous-même.',
                         ' El modelo de NEO no está disponible: añade el texto tú.', ' Il modello di NEO non è disponibile: aggiungi il testo tu.',
                         ' O modelo do NEO não está disponível: acrescenta o texto tu.'),
    'which': ('С каким файлом работать? Откройте его, выделите в Проводнике или назовите («в файле отчёт.xlsx»).',
              'Which file? Open it, select it in File Explorer or name it (“in the file report.xlsx”).',
              'Welche Datei? Öffne sie, markiere sie im Explorer oder nenne sie („in der Datei bericht.xlsx“).',
              'Quel fichier ? Ouvrez-le, sélectionnez-le dans l’Explorateur ou nommez-le (« dans le fichier rapport.xlsx »).',
              '¿Qué archivo? Ábrelo, selecciónalo en el Explorador o nómbralo («en el archivo informe.xlsx»).',
              'Quale file? Aprilo, selezionalo in Esplora file o nominalo («nel file report.xlsx»).',
              'Que ficheiro? Abre-o, seleciona-o no Explorador ou diz o nome («no ficheiro relatorio.xlsx»).'),
    'not_found': ('Файл «{name}» не нашёл в Документах, на Рабочем столе и в Загрузках.', 'Couldn’t find “{name}” in Documents, Desktop or Downloads.',
                  '„{name}“ nicht in Dokumente, Desktop oder Downloads gefunden.', '« {name} » introuvable dans Documents, Bureau ou Téléchargements.',
                  'No encontré «{name}» en Documentos, Escritorio ni Descargas.', '«{name}» non trovato in Documenti, Desktop o Download.',
                  'Não encontrei «{name}» em Documentos, Ambiente de trabalho ou Transferências.'),
    'wrong_kind': ('«{name}» — не {kind}.', '“{name}” is not a {kind}.', '„{name}“ ist kein(e) {kind}.', '« {name} » n’est pas {kind}.', '«{name}» no es {kind}.',
                   '«{name}» non è {kind}.', '«{name}» não é {kind}.'),
    'locked': ('«{name}» открыт в другой программе, и записать в него нельзя. Сохраните и закройте его — и повторите.',
               '“{name}” is open in another program and can’t be written. Save and close it, then try again.',
               '„{name}“ ist in einem anderen Programm geöffnet. Speichere und schließe es, dann erneut.',
               '« {name} » est ouvert dans un autre programme. Enregistrez-le et fermez-le, puis réessayez.',
               '«{name}» está abierto en otro programa. Guárdalo y ciérralo, y vuelve a intentarlo.',
               '«{name}» è aperto in un altro programma. Salvalo e chiudilo, poi riprova.',
               '«{name}» está aberto noutro programa. Guarda-o e fecha-o, e tenta de novo.'),
    'missing_lib': ('Для работы с файлами Office нужен компонент {lib}. Перезапустите NEO — он установится при проверке зависимостей.',
                    'Working with Office files needs the {lib} component. Restart NEO — the dependency check installs it.',
                    'Für Office-Dateien wird {lib} benötigt. Starte NEO neu – die Abhängigkeitsprüfung installiert es.',
                    'Il faut le composant {lib}. Redémarrez NEO — la vérification des dépendances l’installe.',
                    'Hace falta el componente {lib}. Reinicia NEO: la comprobación de dependencias lo instala.',
                    'Serve il componente {lib}. Riavvia NEO: il controllo delle dipendenze lo installa.',
                    'É preciso o componente {lib}. Reinicia o NEO — a verificação de dependências instala-o.'),
    'appended': ('Добавил текст в «{name}».', 'Added the text to “{name}”.', 'Text zu „{name}“ hinzugefügt.', 'Texte ajouté à « {name} ».',
                 'Añadí el texto a «{name}».', 'Testo aggiunto a «{name}».', 'Acrescentei o texto a «{name}».'),
    'heading': ('Добавил заголовок в «{name}».', 'Added the heading to “{name}”.', 'Überschrift zu „{name}“ hinzugefügt.', 'Titre ajouté à « {name} ».',
                'Añadí el título a «{name}».', 'Titolo aggiunto a «{name}».', 'Acrescentei o título a «{name}».'),
    'replaced': ('Заменил «{a}» на «{b}» в «{name}»: {n}.', 'Replaced “{a}” with “{b}” in “{name}”: {n}.', '„{a}“ durch „{b}“ ersetzt in „{name}“: {n}.',
                 '« {a} » remplacé par « {b} » dans « {name} » : {n}.', 'Reemplacé «{a}» por «{b}» en «{name}»: {n}.', 'Sostituito «{a}» con «{b}» in «{name}»: {n}.',
                 'Substituí «{a}» por «{b}» em «{name}»: {n}.'),
    'not_in_doc': ('«{a}» в «{name}» не нашёл.', 'Didn’t find “{a}” in “{name}”.', '„{a}“ nicht in „{name}“ gefunden.', '« {a} » introuvable dans « {name} ».',
                   'No encontré «{a}» en «{name}».', '«{a}» non trovato in «{name}».', 'Não encontrei «{a}» em «{name}».'),
    'cell_set': ('{cell} = {value} («{sheet}», {name}).', '{cell} = {value} (“{sheet}”, {name}).', '{cell} = {value} („{sheet}“, {name}).',
                 '{cell} = {value} (« {sheet} », {name}).', '{cell} = {value} («{sheet}», {name}).', '{cell} = {value} («{sheet}», {name}).', '{cell} = {value} («{sheet}», {name}).'),
    'cell_value': ('В {cell}: {value}', 'In {cell}: {value}', 'In {cell}: {value}', 'Dans {cell} : {value}', 'En {cell}: {value}', 'In {cell}: {value}', 'Em {cell}: {value}'),
    'cell_empty': ('Ячейка {cell} пустая.', 'Cell {cell} is empty.', 'Zelle {cell} ist leer.', 'La cellule {cell} est vide.', 'La celda {cell} está vacía.',
                   'La cella {cell} è vuota.', 'A célula {cell} está vazia.'),
    'bad_cell': ('«{cell}» — не адрес ячейки (пример: B2).', '“{cell}” is not a cell address (e.g. B2).', '„{cell}“ ist keine Zelladresse (z. B. B2).',
                 '« {cell} » n’est pas une adresse de cellule (ex. B2).', '«{cell}» no es una celda (p. ej. B2).', '«{cell}» non è una cella (es. B2).', '«{cell}» não é uma célula (ex. B2).'),
    'stat': ('{stat} столбца {col}: {value} (чисел: {n}).', '{stat} of column {col}: {value} ({n} numbers).', '{stat} der Spalte {col}: {value} ({n} Zahlen).',
             '{stat} de la colonne {col} : {value} ({n} nombres).', '{stat} de la columna {col}: {value} ({n} números).', '{stat} della colonna {col}: {value} ({n} numeri).',
             '{stat} da coluna {col}: {value} ({n} números).'),
    'no_numbers': ('В столбце {col} нет чисел.', 'Column {col} has no numbers.', 'Spalte {col} enthält keine Zahlen.', 'La colonne {col} ne contient pas de nombres.',
                   'La columna {col} no tiene números.', 'La colonna {col} non contiene numeri.', 'A coluna {col} não tem números.'),
    'stat_sum': ('Сумма', 'Sum', 'Summe', 'Somme', 'Suma', 'Somma', 'Soma'),
    'stat_average': ('Среднее', 'Average', 'Durchschnitt', 'Moyenne', 'Promedio', 'Media', 'Média'),
    'stat_max': ('Максимум', 'Maximum', 'Maximum', 'Maximum', 'Máximo', 'Massimo', 'Máximo'),
    'stat_min': ('Минимум', 'Minimum', 'Minimum', 'Minimum', 'Mínimo', 'Minimo', 'Mínimo'),
    'row_added': ('Добавил строку {row} в «{sheet}» ({name}).', 'Added row {row} to “{sheet}” ({name}).', 'Zeile {row} zu „{sheet}“ hinzugefügt ({name}).',
                  'Ligne {row} ajoutée à « {sheet} » ({name}).', 'Añadí la fila {row} a «{sheet}» ({name}).', 'Aggiunta la riga {row} a «{sheet}» ({name}).',
                  'Acrescentei a linha {row} a «{sheet}» ({name}).'),
    'sheet_added': ('Добавил лист «{sheet}» в {name}.', 'Added the sheet “{sheet}” to {name}.', 'Blatt „{sheet}“ zu {name} hinzugefügt.', 'Feuille « {sheet} » ajoutée à {name}.',
                    'Añadí la hoja «{sheet}» a {name}.', 'Aggiunto il foglio «{sheet}» a {name}.', 'Acrescentei a folha «{sheet}» a {name}.'),
    'slide_added': ('Добавил слайд {n} «{title}» в {name}.', 'Added slide {n} “{title}” to {name}.', 'Folie {n} „{title}“ zu {name} hinzugefügt.',
                    'Diapositive {n} « {title} » ajoutée à {name}.', 'Añadí la diapositiva {n} «{title}» a {name}.', 'Aggiunta la diapositiva {n} «{title}» a {name}.',
                    'Acrescentei o diapositivo {n} «{title}» a {name}.'),
    'saved': ('Сохранил «{name}».', 'Saved “{name}”.', '„{name}“ gespeichert.', '« {name} » enregistré.', 'Guardé «{name}».', 'Salvato «{name}».', 'Guardei «{name}».'),
    'exported': ('Сохранил копию: {path}', 'Saved a copy: {path}', 'Kopie gespeichert: {path}', 'Copie enregistrée : {path}', 'Copia guardada: {path}',
                 'Copia salvata: {path}', 'Cópia guardada: {path}'),
    'no_pdf': ('Для PDF нужен установленный Microsoft Office или LibreOffice — ни того, ни другого не нашёл. Можно сохранить в PDF из самой программы: Файл → Сохранить как → PDF.',
               'PDF needs Microsoft Office or LibreOffice installed — found neither. You can save as PDF from the program itself: File → Save as → PDF.',
               'Für PDF wird Microsoft Office oder LibreOffice benötigt – keins gefunden.', 'Le PDF nécessite Microsoft Office ou LibreOffice — aucun trouvé.',
               'El PDF requiere Microsoft Office o LibreOffice: no encontré ninguno.', 'Il PDF richiede Microsoft Office o LibreOffice: nessuno trovato.',
               'O PDF precisa do Microsoft Office ou do LibreOffice — não encontrei nenhum.'),
    'csv_only_sheet': ('В CSV сохраняются только таблицы.', 'Only spreadsheets can be saved as CSV.', 'Nur Tabellen lassen sich als CSV speichern.',
                       'Seuls les classeurs peuvent être enregistrés en CSV.', 'Solo las hojas de cálculo se guardan en CSV.', 'Solo i fogli di calcolo si salvano in CSV.',
                       'Só folhas de cálculo podem ser guardadas em CSV.'),
    'undone': ('Вернул «{name}» как было до моего изменения.', 'Restored “{name}” to how it was before my change.', '„{name}“ wiederhergestellt.',
               '« {name} » restauré tel qu’avant ma modification.', 'Restauré «{name}» como estaba antes de mi cambio.', 'Ripristinato «{name}» com’era prima.',
               'Repus «{name}» como estava antes da minha alteração.'),
    'nothing_to_undo': ('Нечего отменять: я ещё не менял файлы Office.', 'Nothing to undo: I haven’t changed any Office files yet.',
                        'Nichts rückgängig zu machen.', 'Rien à annuler.', 'Nada que deshacer.', 'Niente da annullare.', 'Nada para desfazer.'),
    'live_undo': (' (изменение сделано в открытом окне — его можно отменить там же, Ctrl+Z)', ' (made in the open window — Ctrl+Z there undoes it)',
                  ' (im offenen Fenster – Strg+Z macht es rückgängig)', ' (fait dans la fenêtre ouverte — Ctrl+Z l’annule)', ' (hecho en la ventana abierta: Ctrl+Z lo deshace)',
                  ' (fatto nella finestra aperta: Ctrl+Z lo annulla)', ' (feito na janela aberta — Ctrl+Z desfaz)'),
    'empty': ('«{name}» пустой.', '“{name}” is empty.', '„{name}“ ist leer.', '« {name} » est vide.', '«{name}» está vacío.', '«{name}» è vuoto.', '«{name}» está vazio.'),
    'slides': ('Слайдов: {n}.', 'Slides: {n}.', 'Folien: {n}.', 'Diapositives : {n}.', 'Diapositivas: {n}.', 'Diapositive: {n}.', 'Diapositivos: {n}.'),
    'sheet_rows': ('Лист «{sheet}» ({rows} строк × {cols} столбцов):', 'Sheet “{sheet}” ({rows} rows × {cols} columns):', 'Blatt „{sheet}“ ({rows} Zeilen × {cols} Spalten):',
                   'Feuille « {sheet} » ({rows} lignes × {cols} colonnes) :', 'Hoja «{sheet}» ({rows} filas × {cols} columnas):', 'Foglio «{sheet}» ({rows} righe × {cols} colonne):',
                   'Folha «{sheet}» ({rows} linhas × {cols} colunas):'),
    'no_model': ('Пересказ делает локальная модель NEO, а она сейчас недоступна. Вот начало текста:\n{text}',
                 'Summaries are made by NEO’s local model, which is unavailable now. Here is the beginning of the text:\n{text}',
                 'Zusammenfassungen macht NEOs lokales Modell, das gerade nicht verfügbar ist. Anfang des Textes:\n{text}',
                 'Les résumés sont faits par le modèle local de NEO, indisponible. Début du texte :\n{text}',
                 'Los resúmenes los hace el modelo local de NEO, que no está disponible. Inicio del texto:\n{text}',
                 'I riassunti li fa il modello locale di NEO, ora non disponibile. Inizio del testo:\n{text}',
                 'Os resumos são feitos pelo modelo local do NEO, indisponível agora. Início do texto:\n{text}'),
    'opened': ('Открыл «{name}».', 'Opened “{name}”.', '„{name}“ geöffnet.', '« {name} » ouvert.', 'Abrí «{name}».', 'Aperto «{name}».', 'Abri «{name}».'),
    'unsupported': ('Это работает только в Windows.', 'This works only on Windows.', 'Das geht nur unter Windows.', 'Cela ne fonctionne que sous Windows.',
                    'Esto solo funciona en Windows.', 'Funziona solo su Windows.', 'Isto só funciona no Windows.'),
    'default_title_docx': ('Документ', 'Document', 'Dokument', 'Document', 'Documento', 'Documento', 'Documento'),
    'default_title_xlsx': ('Таблица', 'Spreadsheet', 'Tabelle', 'Classeur', 'Hoja de cálculo', 'Foglio', 'Folha de cálculo'),
    'default_title_pptx': ('Презентация', 'Presentation', 'Präsentation', 'Présentation', 'Presentación', 'Presentazione', 'Apresentação'),
    'slide_n': ('Слайд {n}', 'Slide {n}', 'Folie {n}', 'Diapositive {n}', 'Diapositiva {n}', 'Diapositiva {n}', 'Diapositivo {n}'),
    'sheet_n': ('Лист{n}', 'Sheet{n}', 'Tabelle{n}', 'Feuil{n}', 'Hoja{n}', 'Foglio{n}', 'Folha{n}'),
}


def t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_LANGS.index(_code(code))].format(**values)


@dataclass(slots=True)
class Result:
    ok: bool
    text: str
    data: dict[str, Any] = field(default_factory=dict)


class MissingLibrary(RuntimeError):
    pass


def _lib(name: str):
    try:
        if name == 'docx':
            import docx
            return docx
        if name == 'openpyxl':
            import openpyxl
            return openpyxl
        if name == 'pptx':
            import pptx
            return pptx
    except ImportError as exc:
        raise MissingLibrary({'docx': 'python-docx', 'openpyxl': 'openpyxl', 'pptx': 'python-pptx'}[name]) from exc
    raise MissingLibrary(name)


# ---- which document --------------------------------------------------------------------------------

def remember(path: str | Path, kind: str = '', *, created: bool = False) -> None:
    path = str(path)
    kind = kind or SUFFIX_KIND.get(Path(path).suffix.casefold(), '')
    with _STATE_LOCK:
        _STATE['last'][kind] = (path, time.monotonic())
        _STATE['last']['any'] = (path, time.monotonic())
        if created:
            _STATE['created'].add(os.path.normcase(path))
    try:
        from jarvis.runtime import context173
        context173.note('file', path)
    except Exception:
        pass


def created_by_neo(path: str | Path) -> bool:
    with _STATE_LOCK:
        return os.path.normcase(str(path)) in _STATE['created']


def _recent(kind: str, max_age: float = 2 * 3600) -> str:
    with _STATE_LOCK:
        row = _STATE['last'].get(kind or 'any')
    if row and time.monotonic() - row[1] <= max_age and Path(row[0]).exists():
        return row[0]
    return ''


def user_folders() -> list[Path]:
    home = Path.home()
    folders = []
    try:
        from jarvis.runtime import tidy172
        folders = [tidy172.known_folder(k) for k in ('documents', 'desktop', 'downloads')]
    except Exception:
        folders = [home / 'Documents', home / 'Desktop', home / 'Downloads']
    return [f for f in folders if f]


def find_named(name: str, folders: list[Path] | None = None) -> str:
    """A file by name (with or without folders): the newest match in Documents, Desktop, Downloads."""
    name = str(name or '').strip().strip('«»"')
    if not name:
        return ''
    direct = Path(os.path.expandvars(os.path.expanduser(name)))
    if direct.is_absolute() and direct.exists():
        return str(direct)
    wanted = name.casefold().replace('ё', 'е')
    rows: list[tuple[float, str]] = []
    for folder in folders or user_folders():
        try:
            candidates = list(folder.glob('*')) + list(folder.glob('*/*'))
        except OSError:
            continue
        for path in candidates[:6000]:
            low = path.name.casefold().replace('ё', 'е')
            if low == wanted or (Path(wanted).suffix and low == wanted) or (not Path(wanted).suffix and path.stem.casefold().replace('ё', 'е') == wanted):
                try:
                    rows.append((path.stat().st_mtime, str(path)))
                except OSError:
                    continue
    return max(rows)[1] if rows else ''


# ---- live Office (COM) ------------------------------------------------------------------------------

class Live:
    """The document open in Word / Excel / PowerPoint, changed through Office itself."""

    PROGIDS = {'docx': 'Word.Application', 'xlsx': 'Excel.Application', 'pptx': 'PowerPoint.Application'}

    def __init__(self, kind: str, app: Any, doc: Any) -> None:
        self.kind, self.app, self.doc = kind, app, doc

    @property
    def path(self) -> str:
        try:
            return str(self.doc.FullName)
        except Exception:
            return ''

    @property
    def name(self) -> str:
        try:
            return str(self.doc.Name)
        except Exception:
            return '?'

    @classmethod
    def find(cls, kind: str, path: str = '') -> 'Live | None':
        if os.name != 'nt' or kind not in cls.PROGIDS:
            return None
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            app = win32com.client.GetActiveObject(cls.PROGIDS[kind])
        except Exception:
            return None
        try:
            docs = app.Workbooks if kind == 'xlsx' else app.Documents if kind == 'docx' else app.Presentations
            if path:
                for index in range(1, int(docs.Count) + 1):
                    doc = docs.Item(index)
                    if os.path.normcase(str(doc.FullName)) == os.path.normcase(path):
                        return cls(kind, app, doc)
                return None
            doc = app.ActiveWorkbook if kind == 'xlsx' else app.ActiveDocument if kind == 'docx' else app.ActivePresentation
            return cls(kind, app, doc) if doc is not None else None
        except Exception:
            return None


# ---- file operations ----------------------------------------------------------------------------------

def _backup(config: Any, path: Path) -> None:
    root = Path(getattr(getattr(config, 'paths', None), 'cache', '') or Path(getattr(getattr(config, 'paths', None), 'root', '.')) / 'cache') / 'office_undo'
    root.mkdir(parents=True, exist_ok=True)
    target = root / f'{int(time.time() * 1000)}__{path.name}'
    shutil.copy2(path, target)
    with _STATE_LOCK:
        _STATE['changed'] = time.monotonic()
    (root / (target.name + '.json')).write_text(json.dumps({'path': str(path)}, ensure_ascii=False), encoding='utf-8')
    backups = sorted(p for p in root.iterdir() if p.suffix != '.json')
    for old in backups[:-20]:
        for item in (old, old.with_name(old.name + '.json')):
            try:
                item.unlink()
            except OSError:
                pass


def _latest_backup(config: Any) -> tuple[Path, Path] | None:
    root = Path(getattr(getattr(config, 'paths', None), 'cache', '') or Path(getattr(getattr(config, 'paths', None), 'root', '.')) / 'cache') / 'office_undo'
    if not root.is_dir():
        return None
    for backup in sorted((p for p in root.iterdir() if p.suffix != '.json'), reverse=True):
        meta = backup.with_name(backup.name + '.json')
        try:
            original = Path(json.loads(meta.read_text(encoding='utf-8'))['path'])
        except Exception:
            continue
        return backup, original
    return None


def _drop_latest_backup(config: Any) -> None:
    found = _latest_backup(config)
    if found is not None:
        for item in (found[0], found[0].with_name(found[0].name + '.json')):
            try:
                item.unlink()
            except OSError:
                pass


def _locked(path: Path) -> bool:
    """Open in a program that blocks writing (Office lock file or an exclusive handle)."""
    if (path.parent / ('~$' + path.name[2:])).exists() or (path.parent / ('~$' + path.name)).exists() or (path.parent / ('.~lock.' + path.name + '#')).exists():
        return True
    try:
        with open(path, 'r+b'):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def _cell_ok(cell: str) -> bool:
    return bool(re.fullmatch(r'[A-Z]{1,3}[1-9]\d{0,6}', cell))


def _number(value: str) -> Any:
    text = str(value).strip().replace('\u00a0', '').replace(' ', '')
    if re.fullmatch(r'-?\d+', text):
        return int(text)
    if re.fullmatch(r'-?\d+[.,]\d+', text):
        return float(text.replace(',', '.'))
    return str(value).strip()


def _split(values: str) -> list[str]:
    parts = re.split(r'\s*[;|]\s*', values) if ';' in values or '|' in values else re.split(r'\s*,\s*', values)
    return [p.strip(' .') for p in parts if p.strip(' .')]


def read_text(path: Path, code: str, *, limit: int = 6000) -> str:
    kind = SUFFIX_KIND.get(path.suffix.casefold(), '')
    if kind == 'docx':
        doc = _lib('docx').Document(str(path))
        lines = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                lines.append(' | '.join(cell.text.strip() for cell in row.cells))
        text = '\n'.join(lines)
    elif kind == 'xlsx':
        book = _lib('openpyxl').load_workbook(str(path), read_only=True, data_only=True)
        out = []
        for sheet in book.worksheets:
            rows = [row for row in sheet.iter_rows(values_only=True) if any(v not in (None, '') for v in row)]
            width = max((len(r) for r in rows), default=0)
            out.append(t('sheet_rows', code, sheet=sheet.title, rows=len(rows), cols=width))
            for row in rows[:15]:
                out.append(' | '.join('' if v is None else str(v) for v in row))
        book.close()
        text = '\n'.join(out)
    elif kind == 'pptx':
        deck = _lib('pptx').Presentation(str(path))
        out = [t('slides', code, n=len(deck.slides))]
        for number, slide in enumerate(deck.slides, 1):
            pieces = []
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    pieces.append(shape.text_frame.text.strip().replace('\n', '; '))
            out.append(f'{number}. ' + ' — '.join(pieces))
        text = '\n'.join(out)
    else:
        text = ''
    return text[:limit]


def _safe_name(title: str, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', ' ', str(title or '')).strip(' .') or fallback
    return name[:80]


def _unique(folder: Path, stem: str, suffix: str) -> Path:
    path = folder / f'{stem}{suffix}'
    number = 2
    while path.exists():
        path = folder / f'{stem} ({number}){suffix}'
        number += 1
    return path


def _model_json(ollama: Any, prompt: str, code: str) -> Any:
    if ollama is None or not hasattr(ollama, 'chat_text'):
        return None
    language = {'ru': 'Russian', 'en': 'English', 'de': 'German', 'fr': 'French', 'es': 'Spanish', 'it': 'Italian', 'pt': 'Portuguese'}[_code(code)]
    messages = [{'role': 'system', 'content': f'You write content for office documents in {language}. Reply with JSON only, no explanations.'},
                {'role': 'user', 'content': prompt}]
    try:
        answer = str(ollama.chat_text(messages, num_predict=1200) or '')
    except Exception:
        logger.info('Local model could not write the office content', exc_info=True)
        return None
    match = re.search(r'[\[{].*[\]}]', answer, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def create(config: Any, args: dict[str, Any], code: str, *, ollama: Any = None, folder: Path | None = None, open_file: Callable[[str], None] | None = None) -> Result:
    kind = str(args.get('kind', 'docx'))
    title = str(args.get('title', '') or '').strip()
    topic = str(args.get('topic', '') or '').strip()
    text = str(args.get('text', '') or '').strip()
    folder = folder or user_folders()[0]
    folder.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(title or topic, t('default_title_' + kind, code) + time.strftime(' %Y-%m-%d %H-%M'))
    path = _unique(folder, stem, KINDS[kind])
    note = ''
    if kind == 'docx':
        docx = _lib('docx')
        doc = docx.Document()
        if title or topic:
            doc.add_heading(title or topic[:1].upper() + topic[1:], level=1)
        paragraphs = [p for p in re.split(r'\n+', text) if p.strip()] if text else []
        if topic and not text:
            data = _model_json(ollama, f'Write a short document about: {topic}. JSON: {{"paragraphs": ["...", "..."]}} with 4-6 paragraphs.', code)
            paragraphs = [str(p) for p in (data or {}).get('paragraphs', [])][:12] if isinstance(data, dict) else []
            note = t('outline_note' if paragraphs else 'no_model_outline', code)
        for paragraph in paragraphs:
            doc.add_paragraph(paragraph)
        doc.save(str(path))
    elif kind == 'xlsx':
        book = _lib('openpyxl').Workbook()
        sheet = book.active
        sheet.title = (title or t('sheet_n', code, n=1))[:31]
        headers = _split(str(args.get('values', '') or ''))
        if headers:
            sheet.append(headers)
            from openpyxl.styles import Font
            for cell in sheet[1]:
                cell.font = Font(bold=True)
            for index, header in enumerate(headers, 1):
                sheet.column_dimensions[sheet.cell(1, index).column_letter].width = max(10, min(40, len(header) + 4))
        book.save(str(path))
    else:
        pptx = _lib('pptx')
        deck = pptx.Presentation()
        count = max(1, min(30, int(args.get('count', 0) or 0))) if args.get('count') else 0
        first = deck.slides.add_slide(deck.slide_layouts[0])
        first.shapes.title.text = title or (topic[:1].upper() + topic[1:] if topic else t('default_title_pptx', code))
        if text and len(first.placeholders) > 1:
            first.placeholders[1].text = text
        slides: list[dict[str, Any]] = []
        if topic:
            data = _model_json(ollama, f'Make a presentation outline about: {topic}. {max(3, count or 5)} slides after the title slide. '
                                       'JSON: {"slides": [{"title": "...", "bullets": ["...", "..."]}]}, 3-5 short bullets each.', code)
            slides = [s for s in (data or {}).get('slides', []) if isinstance(s, dict)][:30] if isinstance(data, dict) else []
            note = t('outline_note' if slides else 'no_model_outline', code)
        if not slides and count > 1:
            slides = [{'title': t('slide_n', code, n=n), 'bullets': []} for n in range(2, count + 1)]
        for row in slides:
            slide = deck.slides.add_slide(deck.slide_layouts[1])
            slide.shapes.title.text = str(row.get('title', ''))[:200]
            bullets = [str(b) for b in row.get('bullets', []) if str(b).strip()]
            if bullets:
                frame = slide.placeholders[1].text_frame
                frame.text = bullets[0]
                for bullet in bullets[1:8]:
                    frame.add_paragraph().text = bullet
        deck.save(str(path))
    remember(path, kind, created=True)
    opened = False
    if bool(_get(config, 'office.open_after_create', True)):
        try:
            (open_file or _open)(str(path))
            opened = True
        except Exception:
            logger.debug('Could not open the new office file', exc_info=True)
    key = 'created' if opened else 'created_closed'
    return Result(True, t(key, code, kind=t('kind_' + kind, code), name=path.name) + note, {'path': str(path), 'kind': kind})


def _get(config: Any, key: str, default: Any) -> Any:
    getter = getattr(config, 'get', None)
    try:
        return getter(key, default) if callable(getter) else default
    except Exception:
        return default


def _open(path: str) -> None:
    os.startfile(path)  # type: ignore[attr-defined]


# ---- running an action --------------------------------------------------------------------------------

_KIND_FOR_ACTION = {'append': 'docx', 'heading': 'docx', 'set_cell': 'xlsx', 'get_cell': 'xlsx', 'column_stat': 'xlsx', 'add_row': 'xlsx',
                    'add_sheet': 'xlsx', 'add_slide': 'pptx'}


@dataclass(slots=True)
class Target:
    path: Path | None
    live: Live | None
    kind: str


def resolve(config: Any, args: dict[str, Any], kind: str, *, live_finder: Callable[[str, str], Live | None] = Live.find,
            selection: Callable[[], str] | None = None) -> Target | None:
    name = str(args.get('name', '') or '')
    if name:
        found = find_named(name)
        if not found:
            return None
        path = Path(found)
        kind = SUFFIX_KIND.get(path.suffix.casefold(), kind)
        return Target(path, live_finder(kind, str(path)), kind)
    kinds = [kind] if kind else ['xlsx', 'docx', 'pptx']
    if not kind:                                  # «замени …», «сохрани в pdf»: the document NEO worked with last
        recent = _recent('any')
        each = SUFFIX_KIND.get(Path(recent).suffix.casefold(), '') if recent else ''
        if each:
            return Target(Path(recent), live_finder(each, recent), each)
    for each in kinds:
        recent = _recent(each)
        if recent:
            return Target(Path(recent), live_finder(each, recent), each)
    for each in kinds:
        live = live_finder(each, '')
        if live is not None and live.path:
            return Target(Path(live.path) if Path(live.path).exists() else None, live, each)
    picked = (selection or _explorer_selection)()
    if picked and SUFFIX_KIND.get(Path(picked).suffix.casefold()) in kinds:
        return Target(Path(picked), live_finder(SUFFIX_KIND[Path(picked).suffix.casefold()], picked), SUFFIX_KIND[Path(picked).suffix.casefold()])
    return None


def _explorer_selection() -> str:
    try:
        from jarvis.runtime import context173
        _folder, selected = context173.explorer_state()
        return selected[0] if len(selected) == 1 else ''
    except Exception:
        return ''


def run(config: Any, args: dict[str, Any], code: str, *, ollama: Any = None, live_finder: Callable[[str, str], Live | None] = Live.find,
        selection: Callable[[], str] | None = None, open_file: Callable[[str], None] | None = None, folder: Path | None = None) -> Result:
    action = str(args.get('action', '') or '')
    try:
        if action == 'create':
            return create(config, args, code, ollama=ollama, folder=folder, open_file=open_file)
        if action == 'undo':
            return _undo(config, code)
        kind = _KIND_FOR_ACTION.get(action) or str(args.get('kind', '') or '')
        target = resolve(config, args, kind, live_finder=live_finder, selection=selection)
        if target is None:
            name = str(args.get('name', '') or '')
            return Result(False, t('not_found', code, name=name) if name else t('which', code), {})
        if action == 'open':
            (open_file or _open)(str(target.path))
            remember(target.path, target.kind)
            return Result(True, t('opened', code, name=target.path.name), {'path': str(target.path)})
        need = _KIND_FOR_ACTION.get(action)
        if need and target.kind != need:
            label = t('kind_' + need, code)
            return Result(False, t('wrong_kind', code, name=(target.path.name if target.path else target.live.name), kind=label), {})
        if target.live is not None:
            return _run_live(target, action, args, code, config=config, ollama=ollama)
        return _run_file(config, target, action, args, code, ollama=ollama)
    except MissingLibrary as exc:
        return Result(False, t('missing_lib', code, lib=str(exc)), {})


def _run_file(config: Any, target: Target, action: str, args: dict[str, Any], code: str, *, ollama: Any = None) -> Result:
    path = target.path
    assert path is not None
    name = path.name
    if action in {'read', 'summarize'}:
        text = read_text(path, code)
        remember(path, target.kind)
        if not text.strip():
            return Result(True, t('empty', code, name=name), {})
        if action == 'read':
            return Result(True, f'«{name}»\n{text}', {'path': str(path)})
        return _summarize(text, code, ollama)
    if action == 'export':
        return _export(path, target.kind, str(args.get('format', 'pdf')), code)
    if action == 'save':
        return Result(True, t('saved', code, name=name), {})
    if action == 'get_cell':
        cell = str(args.get('cell', '')).upper()
        if not _cell_ok(cell):
            return Result(False, t('bad_cell', code, cell=cell), {})
        book = _lib('openpyxl').load_workbook(str(path), data_only=True)
        value = book.active[cell].value
        remember(path, 'xlsx')
        return Result(True, t('cell_value', code, cell=cell, value=value) if value not in (None, '') else t('cell_empty', code, cell=cell), {'value': value})
    if action == 'column_stat':
        book = _lib('openpyxl').load_workbook(str(path), data_only=True)
        sheet = book.active
        column = str(args.get('column', 'A')).upper()
        numbers = [float(c.value) for c in sheet[column] if isinstance(c.value, (int, float)) and not isinstance(c.value, bool)]
        remember(path, 'xlsx')
        return _stat_result(numbers, str(args.get('stat', 'sum')), column, code)
    # changes
    if _locked(path):
        return Result(False, t('locked', code, name=name), {})
    _backup(config, path)
    if action in {'append', 'heading'}:
        doc = _lib('docx').Document(str(path))
        if action == 'heading':
            doc.add_heading(str(args.get('text', '')), level=2)
        else:
            for paragraph in re.split(r'\n+', str(args.get('text', ''))):
                if paragraph.strip():
                    doc.add_paragraph(paragraph)
        doc.save(str(path))
        text = t('heading' if action == 'heading' else 'appended', code, name=name)
    elif action == 'replace':
        a, b = str(args.get('find', '')), str(args.get('text', ''))
        count = _replace_in_file(path, target.kind, a, b)
        if not count:
            _drop_latest_backup(config)          # nothing changed: nothing to undo
            return Result(False, t('not_in_doc', code, a=a, name=name), {})
        text = t('replaced', code, a=a, b=b, name=name, n=count)
    elif action == 'set_cell':
        cell = str(args.get('cell', '')).upper()
        if not _cell_ok(cell):
            return Result(False, t('bad_cell', code, cell=cell), {})
        book = _lib('openpyxl').load_workbook(str(path))
        sheet = book.active
        sheet[cell] = _number(str(args.get('value', '')))
        book.save(str(path))
        text = t('cell_set', code, cell=cell, value=args.get('value', ''), sheet=sheet.title, name=name)
    elif action == 'add_row':
        book = _lib('openpyxl').load_workbook(str(path))
        sheet = book.active
        values = [_number(v) for v in _split(str(args.get('values', '')))]
        sheet.append(values)
        book.save(str(path))
        text = t('row_added', code, row=sheet.max_row, sheet=sheet.title, name=name)
    elif action == 'add_sheet':
        book = _lib('openpyxl').load_workbook(str(path))
        title = (str(args.get('title', '') or '') or t('sheet_n', code, n=len(book.sheetnames) + 1))[:31]
        book.create_sheet(title)
        book.save(str(path))
        text = t('sheet_added', code, sheet=title, name=name)
    elif action == 'add_slide':
        deck = _lib('pptx').Presentation(str(path))
        title, items = _slide_args(args, code, len(deck.slides) + 1)
        slide = deck.slides.add_slide(deck.slide_layouts[1] if len(deck.slide_layouts) > 1 else deck.slide_layouts[0])
        if slide.shapes.title is not None:
            slide.shapes.title.text = title
        if items and len(slide.placeholders) > 1:
            frame = slide.placeholders[1].text_frame
            frame.text = items[0]
            for item in items[1:10]:
                frame.add_paragraph().text = item
        deck.save(str(path))
        text = t('slide_added', code, n=len(deck.slides), title=title, name=name)
    else:
        return Result(False, t('unsupported', code), {})
    remember(path, target.kind)
    logger.info('Office file changed: action=%s kind=%s', action, target.kind)          # never the document text
    return Result(True, text, {'path': str(path)})


def _slide_args(args: dict[str, Any], code: str, number: int) -> tuple[str, list[str]]:
    title = str(args.get('title', '') or '').strip() or t('slide_n', code, n=number)
    items = [i.strip() for i in re.split(r'\s*[;|]\s*|\s*,\s*(?=\S)', str(args.get('items', '') or '')) if i.strip()]
    return title, items


def _replace_in_file(path: Path, kind: str, a: str, b: str) -> int:
    count = 0
    if kind == 'docx':
        doc = _lib('docx').Document(str(path))
        paragraphs = list(doc.paragraphs)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    paragraphs.extend(cell.paragraphs)
        for paragraph in paragraphs:
            if a in paragraph.text:
                count += paragraph.text.count(a)
                done = False
                for run in paragraph.runs:                     # keep formatting when the words sit in one run
                    if a in run.text:
                        run.text = run.text.replace(a, b)
                        done = True
                if not done or a in paragraph.text:
                    paragraph.text = paragraph.text.replace(a, b)
        if count:
            doc.save(str(path))
    elif kind == 'pptx':
        deck = _lib('pptx').Presentation(str(path))
        for slide in deck.slides:
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        if a in run.text:
                            count += run.text.count(a)
                            run.text = run.text.replace(a, b)
        if count:
            deck.save(str(path))
    elif kind == 'xlsx':
        book = _lib('openpyxl').load_workbook(str(path))
        for sheet in book.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and a in cell.value:
                        count += cell.value.count(a)
                        cell.value = cell.value.replace(a, b)
        if count:
            book.save(str(path))
    return count


def _stat_result(numbers: list[float], stat: str, column: str, code: str) -> Result:
    if not numbers:
        return Result(False, t('no_numbers', code, col=column), {})
    value = {'sum': sum(numbers), 'average': sum(numbers) / len(numbers), 'max': max(numbers), 'min': min(numbers)}.get(stat, sum(numbers))
    shown = f'{value:,.2f}'.rstrip('0').rstrip('.').replace(',', ' ')
    return Result(True, t('stat', code, stat=t('stat_' + stat, code), col=column, value=shown, n=len(numbers)), {'value': value})


def _summarize(text: str, code: str, ollama: Any) -> Result:
    from jarvis.runtime import explain172
    if ollama is None or not hasattr(ollama, 'chat_text'):
        return Result(True, t('no_model', code, text=text[:1200]), {})
    answer, _streamed = explain172.run('summarize', explain172.Taken(text, 'page'), code, ollama=ollama)
    return Result(True, answer, {})


def _export(path: Path, kind: str, fmt: str, code: str) -> Result:
    if fmt == 'csv':
        if kind != 'xlsx':
            return Result(False, t('csv_only_sheet', code), {})
        import csv
        book = _lib('openpyxl').load_workbook(str(path), data_only=True, read_only=True)
        target = _unique(path.parent, path.stem, '.csv')
        with open(target, 'w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.writer(handle, delimiter=';')
            for row in book.active.iter_rows(values_only=True):
                writer.writerow(['' if v is None else v for v in row])
        book.close()
        return Result(True, t('exported', code, path=str(target)), {'path': str(target)})
    target = _unique(path.parent, path.stem, '.pdf')
    live = Live.find(kind, str(path))
    try:
        if live is not None:
            _live_pdf(live, target)
            return Result(True, t('exported', code, path=str(target)), {'path': str(target)})
    except Exception:
        logger.debug('Office PDF export failed', exc_info=True)
    soffice = shutil.which('soffice') or next((str(p) for p in (Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'LibreOffice/program/soffice.exe',)
                                              if p.exists()), '')
    if soffice:
        import subprocess
        proc = subprocess.run([soffice, '--headless', '--convert-to', 'pdf', '--outdir', str(path.parent), str(path)], capture_output=True, timeout=120,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        produced = path.with_suffix('.pdf')
        if proc.returncode == 0 and produced.exists():
            return Result(True, t('exported', code, path=str(produced)), {'path': str(produced)})
    office = _office_app(kind)
    if office is not None:
        try:
            _office_pdf(office, kind, path, target)
            return Result(True, t('exported', code, path=str(target)), {'path': str(target)})
        except Exception:
            logger.debug('Office automation PDF export failed', exc_info=True)
    return Result(False, t('no_pdf', code), {})


def _office_app(kind: str) -> Any:
    if os.name != 'nt':
        return None
    try:
        import win32com.client
        return win32com.client.DispatchEx(Live.PROGIDS[kind])
    except Exception:
        return None


def _office_pdf(app: Any, kind: str, path: Path, target: Path) -> None:
    if kind == 'docx':
        doc = app.Documents.Open(str(path), ReadOnly=True)
        doc.ExportAsFixedFormat(str(target), 17)
        doc.Close(False)
    elif kind == 'xlsx':
        book = app.Workbooks.Open(str(path), ReadOnly=True)
        book.ExportAsFixedFormat(0, str(target))
        book.Close(False)
    else:
        deck = app.Presentations.Open(str(path), ReadOnly=True, WithWindow=False)
        deck.SaveAs(str(target), 32)
        deck.Close()
    try:
        app.Quit()
    except Exception:
        pass


def _live_pdf(live: Live, target: Path) -> None:
    if live.kind == 'docx':
        live.doc.ExportAsFixedFormat(str(target), 17)
    elif live.kind == 'xlsx':
        live.doc.ExportAsFixedFormat(0, str(target))
    else:
        live.doc.SaveAs(str(target), 32)


def _run_live(target: Target, action: str, args: dict[str, Any], code: str, *, config: Any, ollama: Any = None) -> Result:
    """The same actions in the open Word / Excel / PowerPoint window (Ctrl+Z there undoes them)."""
    live = target.live
    assert live is not None
    name = live.name
    tail = t('live_undo', code)
    if live.path:
        remember(live.path, live.kind)
    try:
        if action in {'read', 'summarize'}:
            if target.path is not None and target.path.exists() and action == 'read':
                text = read_text(target.path, code)
            elif live.kind == 'docx':
                text = str(live.doc.Content.Text)[:6000]
            elif live.kind == 'xlsx':
                used = live.doc.ActiveSheet.UsedRange.Value
                rows = used if isinstance(used, tuple) else ((used,),)
                text = '\n'.join(' | '.join('' if v is None else str(v) for v in row) for row in rows[:20])
            else:
                text = '\n'.join(f'{i}. ' + ' — '.join(str(s.TextFrame.TextRange.Text) for s in slide.Shapes if s.HasTextFrame)
                                 for i, slide in enumerate(live.doc.Slides, 1))
            if action == 'read':
                return Result(True, f'«{name}»\n{text}', {})
            return _summarize(text, code, ollama)
        if action == 'save':
            live.doc.Save()
            return Result(True, t('saved', code, name=name), {})
        if action == 'export':
            if str(args.get('format')) == 'csv' and target.path is not None:
                return _export(target.path, live.kind, 'csv', code)
            out = _unique(Path(live.path).parent if live.path else user_folders()[0], Path(name).stem, '.pdf')
            _live_pdf(live, out)
            return Result(True, t('exported', code, path=str(out)), {'path': str(out)})
        if live.kind == 'xlsx':
            sheet = live.doc.ActiveSheet
            if action == 'set_cell':
                cell = str(args.get('cell', '')).upper()
                if not _cell_ok(cell):
                    return Result(False, t('bad_cell', code, cell=cell), {})
                sheet.Range(cell).Value = _number(str(args.get('value', '')))
                return Result(True, t('cell_set', code, cell=cell, value=args.get('value', ''), sheet=sheet.Name, name=name) + tail, {})
            if action == 'get_cell':
                cell = str(args.get('cell', '')).upper()
                value = sheet.Range(cell).Value if _cell_ok(cell) else None
                return Result(True, t('cell_value', code, cell=cell, value=value) if value not in (None, '') else t('cell_empty', code, cell=cell), {'value': value})
            if action == 'column_stat':
                column = str(args.get('column', 'A')).upper()
                values = sheet.Range(f'{column}1:{column}{max(1, int(sheet.UsedRange.Rows.Count) + int(sheet.UsedRange.Row))}').Value
                numbers = [float(v[0]) for v in (values or ()) if isinstance(v, tuple) and isinstance(v[0], (int, float)) and not isinstance(v[0], bool)]
                return _stat_result(numbers, str(args.get('stat', 'sum')), column, code)
            if action == 'add_row':
                values = [_number(v) for v in _split(str(args.get('values', '')))]
                row = int(sheet.Cells(sheet.Rows.Count, 1).End(-4162).Row) + 1        # xlUp
                if row == 2 and sheet.Cells(1, 1).Value in (None, ''):
                    row = 1
                for column, value in enumerate(values, 1):
                    sheet.Cells(row, column).Value = value
                return Result(True, t('row_added', code, row=row, sheet=sheet.Name, name=name) + tail, {})
            if action == 'add_sheet':
                new = live.doc.Worksheets.Add(After=live.doc.Worksheets(live.doc.Worksheets.Count))
                title = str(args.get('title', '') or '')[:31]
                if title:
                    new.Name = title
                return Result(True, t('sheet_added', code, sheet=new.Name, name=name) + tail, {})
            if action == 'replace':
                found = sheet.UsedRange.Replace(str(args.get('find', '')), str(args.get('text', '')))
                return Result(bool(found), (t('replaced', code, a=args.get('find'), b=args.get('text'), name=name, n='✓') + tail) if found
                              else t('not_in_doc', code, a=args.get('find'), name=name), {})
        if live.kind == 'docx':
            content = live.doc.Content
            if action in {'append', 'heading'}:
                content.InsertParagraphAfter()
                paragraph = live.doc.Paragraphs(live.doc.Paragraphs.Count)
                paragraph.Range.Text = str(args.get('text', ''))
                if action == 'heading':
                    paragraph.Style = live.doc.Styles(-3)        # wdStyleHeading2
                return Result(True, t('heading' if action == 'heading' else 'appended', code, name=name) + tail, {})
            if action == 'replace':
                finder = content.Find
                found = finder.Execute(FindText=str(args.get('find', '')), ReplaceWith=str(args.get('text', '')), Replace=2)
                return Result(bool(found), (t('replaced', code, a=args.get('find'), b=args.get('text'), name=name, n='✓') + tail) if found
                              else t('not_in_doc', code, a=args.get('find'), name=name), {})
        if live.kind == 'pptx' and action == 'add_slide':
            title, items = _slide_args(args, code, int(live.doc.Slides.Count) + 1)
            slide = live.doc.Slides.Add(int(live.doc.Slides.Count) + 1, 2)        # ppLayoutText
            slide.Shapes(1).TextFrame.TextRange.Text = title
            if items and int(slide.Shapes.Count) > 1:
                slide.Shapes(2).TextFrame.TextRange.Text = '\r'.join(items)
            return Result(True, t('slide_added', code, n=int(live.doc.Slides.Count), title=title, name=name) + tail, {})
        if live.kind == 'pptx' and action == 'replace':
            count = 0
            for slide in live.doc.Slides:
                for shape in slide.Shapes:
                    if shape.HasTextFrame:
                        rng = shape.TextFrame.TextRange
                        if str(args.get('find', '')) in str(rng.Text):
                            rng.Replace(str(args.get('find', '')), str(args.get('text', '')))
                            count += 1
            return Result(bool(count), (t('replaced', code, a=args.get('find'), b=args.get('text'), name=name, n=count) + tail) if count
                          else t('not_in_doc', code, a=args.get('find'), name=name), {})
    except Exception:
        logger.info('Office automation failed: action=%s kind=%s', action, live.kind, exc_info=True)
        if target.path is not None and target.path.exists():
            return Result(False, t('locked', code, name=name), {})
    return Result(False, t('unsupported', code), {})


def _undo(config: Any, code: str) -> Result:
    found = _latest_backup(config)
    if found is None:
        return Result(False, t('nothing_to_undo', code), {})
    backup, original = found
    if original.exists() and _locked(original):
        return Result(False, t('locked', code, name=original.name), {})
    shutil.copy2(backup, original)
    for item in (backup, backup.with_name(backup.name + '.json')):
        try:
            item.unlink()
        except OSError:
            pass
    return Result(True, t('undone', code, name=original.name), {'path': str(original)})


def claims(args: dict[str, Any], *, live_finder: Callable[[str, str], 'Live | None'] | None = None) -> bool:
    """Whether a 'soft' request (no document named in the phrase) really is about an Office document:
    a file named in it, a document NEO worked with in the last two hours, or one open in Office now."""
    if not args.get('soft'):
        return True
    action = str(args.get('action', '') or '')
    if action == 'undo':
        with _STATE_LOCK:
            changed = _STATE.get('changed')
        return changed is not None and time.monotonic() - changed <= 2 * 3600
    if args.get('name'):
        return True
    kind = _KIND_FOR_ACTION.get(action) or str(args.get('kind', '') or '')
    kinds = [kind] if kind else ['docx', 'xlsx', 'pptx']
    if any(_recent(each) for each in kinds):
        return True
    finder = live_finder or Live.find
    try:
        return any(finder(each, '') is not None for each in kinds)
    except Exception:
        return False


def risk(args: dict[str, Any], config: Any = None) -> str:
    """'safe' | 'medium': changing a file NEO did not create asks first (a copy is kept for undo anyway)."""
    action = str(args.get('action', '') or '')
    if action in {'chart', 'copy_formula'}:
        return 'medium'     # NEO 188 NEO-OFFICE-001: a new file is written only after the shown preview
    if action in {'create', 'read', 'summarize', 'get_cell', 'column_stat', 'export', 'save', 'open'}:
        return 'safe'
    if action == 'undo':
        return 'medium'
    name = str(args.get('name', '') or '')
    if name:
        path = find_named(name)
        return 'safe' if path and created_by_neo(path) else 'medium'
    recent = _recent(_KIND_FOR_ACTION.get(action, '') or str(args.get('kind', '') or ''))
    if recent and created_by_neo(recent):
        return 'safe'
    return 'safe' if any(Live.find(k, '') is not None for k in ([_KIND_FOR_ACTION[action]] if action in _KIND_FOR_ACTION else ['docx', 'xlsx', 'pptx'])) else 'medium'


__all__ = ['KINDS', 'Live', 'Result', 'claims', 'create', 'created_by_neo', 'find_named', 'parse', 'read_text', 'remember', 'resolve', 'risk', 'run', 't']
