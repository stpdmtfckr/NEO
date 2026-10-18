"""NEO 188 NEO-DOC-001 / TZ-LOCAL-DOCUMENT-EXPLICIT-SCOPE-01.

«Ответь по файлу отчёт.pdf: когда поездка?» answers from THAT file only;
«сравни файлы план.docx и бюджет.xlsx по срокам» compares exactly two files.

* The file filter is part of the index query (before ranking and LIMIT); a
  file name in the question or «use only A» in a prompt is never the filter.
* Only files that are already allowed AND indexed are used; choosing a file
  never adds its folder to the allowed list.
* Same names in different folders, a third file or the same file twice →
  a question; an unknown, changed or deleted file → an honest limitation.
* Each side of a comparison gets its own evidence; a side without evidence is
  named and no comparison is invented.
* Document text stays data: instructions inside a file do not change the
  chosen set or start anything (fragments only go to the answer prompt).
"""
from __future__ import annotations

import ntpath
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jarvis.runtime.natural_commands import LANGUAGES, strip_request_shell

_Q = r'(?P<q>.+)'
_F = r'(?P<file>«[^»]+»|"[^"]+"|[^:,;?]+?)'
_A = r'(?P<a>«[^»]+»|"[^"]+"|\S+?(?:\s\S+?)??)'
_B = r'(?P<b>«[^»]+»|"[^"]+"|\S+?(?:\s\S+?)??)'
_ASK = {
    'ru': (r'(?:ответь|скажи|расскажи)?\s*(?:только\s+)?по\s+(?:файлу|документу)\s+' + _F + r'\s*[:,]\s*' + _Q,
           r'что\s+(?:в\s+файле|в\s+документе)\s+' + _F + r'\s+(?:сказано|написано|говорится)\s+(?:о|об|про)\s+' + _Q),
    'en': (r'(?:answer\s+)?(?:only\s+)?(?:from|using|based\s+on)\s+(?:the\s+)?(?:file|document)\s+' + _F + r'\s*[:,]\s*' + _Q,
           r'what\s+does\s+(?:the\s+)?(?:file|document)\s+' + _F + r'\s+say\s+about\s+' + _Q),
    'de': (r'(?:antworte\s+)?(?:nur\s+)?(?:aus|anhand)\s+(?:der\s+datei|des\s+dokuments|dem\s+dokument)\s+' + _F + r'\s*[:,]\s*' + _Q,
           r'was\s+steht\s+in\s+(?:der\s+datei|dem\s+dokument)\s+' + _F + r'\s+(?:über|zu)\s+' + _Q),
    'fr': (r'(?:réponds\s+)?(?:seulement\s+)?(?:d[’\']après|selon)\s+le\s+(?:fichier|document)\s+' + _F + r'\s*[:,]\s*' + _Q,
           r'que\s+dit\s+le\s+(?:fichier|document)\s+' + _F + r'\s+(?:sur|à\s+propos\s+de)\s+' + _Q),
    'es': (r'(?:responde\s+)?(?:solo\s+)?(?:según|con)\s+el\s+(?:archivo|documento)\s+' + _F + r'\s*[:,]\s*' + _Q,
           r'qué\s+dice\s+el\s+(?:archivo|documento)\s+' + _F + r'\s+(?:sobre|de)\s+' + _Q),
    'it': (r'(?:rispondi\s+)?(?:solo\s+)?(?:secondo|in\s+base\s+al)\s+(?:il\s+)?(?:file|documento)\s+' + _F + r'\s*[:,]\s*' + _Q,
           r'cosa\s+dice\s+il\s+(?:file|documento)\s+' + _F + r'\s+(?:su|riguardo\s+a)\s+' + _Q),
    'pt': (r'(?:responde\s+)?(?:só\s+)?(?:segundo|com\s+base\s+n)o\s+(?:ficheiro|arquivo|documento)\s+' + _F + r'\s*[:,]\s*' + _Q,
           r'o\s+que\s+diz\s+o\s+(?:ficheiro|arquivo|documento)\s+' + _F + r'\s+sobre\s+' + _Q),
}
_COMPARE = {
    'ru': r'сравни\s+(?:файлы|документы)\s+' + _A + r'\s+и\s+' + _B + r'(?:\s+(?:по|о|об|про|насчёт|насчет)\s+' + _Q + r')?',
    'en': r'compare\s+(?:the\s+)?(?:files|documents)\s+' + _A + r'\s+and\s+' + _B + r'(?:\s+(?:on|about|regarding|for)\s+' + _Q + r')?',
    'de': r'vergleiche\s+(?:die\s+)?(?:dateien|dokumente)\s+' + _A + r'\s+und\s+' + _B + r'(?:\s+(?:bezüglich|zu|über|hinsichtlich)\s+' + _Q + r')?',
    'fr': r'compare\s+(?:les\s+)?(?:fichiers|documents)\s+' + _A + r'\s+et\s+' + _B + r'(?:\s+(?:sur|à\s+propos\s+de|concernant)\s+' + _Q + r')?',
    'es': r'compara\s+(?:los\s+)?(?:archivos|documentos)\s+' + _A + r'\s+y\s+' + _B + r'(?:\s+(?:sobre|en\s+cuanto\s+a|respecto\s+a)\s+' + _Q + r')?',
    'it': r'confronta\s+(?:i\s+)?(?:file|documenti)\s+' + _A + r'\s+e\s+' + _B + r'(?:\s+(?:su|riguardo\s+a|per)\s+' + _Q + r')?',
    'pt': r'compara\s+(?:os\s+)?(?:ficheiros|arquivos|documentos)\s+' + _A + r'\s+e\s+' + _B + r'(?:\s+(?:sobre|quanto\s+a|em\s+relação\s+a)\s+' + _Q + r')?',
}


def _unquote(value: str) -> str:
    return str(value or '').strip().strip('«»"“”').strip()


@lru_cache(maxsize=7)
def _patterns(code: str):
    return tuple(re.compile(r'^' + p + r'$', re.I) for p in _ASK[code]), re.compile(r'^' + _COMPARE[code] + r'$', re.I)


def parse(text: str, code: str) -> tuple[str, dict] | None:
    if code not in LANGUAGES:
        return None
    value = strip_request_shell(str(text or ''), code).strip(' .!?¿¡')
    asks, compare = _patterns(code)
    match = compare.fullmatch(value)
    if match:
        args = {'action': 'compare', 'file_a': _unquote(match.group('a')), 'file_b': _unquote(match.group('b'))}
        if match.group('q'):
            args['query'] = match.group('q').strip()
        return 'knowledge', args
    for pattern in asks:
        match = pattern.fullmatch(value)
        if match:
            return 'knowledge', {'action': 'ask_file', 'file': _unquote(match.group('file')), 'query': match.group('q').strip()}
    return None


_ORDER = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_TEXT = {
    'scope': ('Источник ответа: {a}.', 'Answer source: {a}.', 'Quelle der Antwort: {a}.', 'Source de la réponse : {a}.',
              'Fuente de la respuesta: {a}.', 'Fonte della risposta: {a}.', 'Fonte da resposta: {a}.'),
    'scope_pair': ('Сравниваю: A — {a}; B — {b}.', 'Comparing: A — {a}; B — {b}.', 'Vergleich: A — {a}; B — {b}.',
                   'Comparaison : A — {a} ; B — {b}.', 'Comparo: A — {a}; B — {b}.', 'Confronto: A — {a}; B — {b}.',
                   'A comparar: A — {a}; B — {b}.'),
    'unknown': ('Файл «{name}» не найден среди разрешённых и проиндексированных файлов. Разрешите поиск в его папке — сам выбор файла доступ не открывает.',
                'The file “{name}” is not among the allowed, indexed files. Allow searching its folder — choosing a file does not grant access.',
                'Die Datei „{name}“ ist nicht unter den erlaubten, indizierten Dateien. Erlaube die Suche in ihrem Ordner – die Auswahl allein gibt keinen Zugriff.',
                'Le fichier « {name} » ne fait pas partie des fichiers autorisés et indexés. Autorisez la recherche dans son dossier : le choisir ne donne pas l’accès.',
                'El archivo «{name}» no está entre los archivos permitidos e indexados. Permite buscar en su carpeta: elegirlo no da acceso.',
                'Il file «{name}» non è tra i file consentiti e indicizzati. Consenti la ricerca nella sua cartella: sceglierlo non dà accesso.',
                'O ficheiro «{name}» não está entre os ficheiros permitidos e indexados. Permite procurar na pasta dele: escolhê-lo não dá acesso.'),
    'ambiguous': ('Файлов с именем «{name}» несколько. Какой взять? Назовите путь:\n{choices}',
                  'There are several files named “{name}”. Which one? Give its path:\n{choices}',
                  'Es gibt mehrere Dateien „{name}“. Welche? Nenne den Pfad:\n{choices}',
                  'Plusieurs fichiers s’appellent « {name} ». Lequel ? Indiquez le chemin :\n{choices}',
                  'Hay varios archivos llamados «{name}». ¿Cuál? Di la ruta:\n{choices}',
                  'Ci sono più file chiamati «{name}». Quale? Indica il percorso:\n{choices}',
                  'Há vários ficheiros chamados «{name}». Qual? Diz o caminho:\n{choices}'),
    'same': ('Для сравнения нужны два разных файла, а указан один и тот же: {a}.', 'A comparison needs two different files; both name {a}.',
             'Für einen Vergleich braucht es zwei verschiedene Dateien; beide sind {a}.', 'Une comparaison demande deux fichiers différents ; les deux sont {a}.',
             'Una comparación necesita dos archivos distintos; los dos son {a}.', 'Un confronto richiede due file diversi; entrambi sono {a}.',
             'Uma comparação precisa de dois ficheiros diferentes; ambos são {a}.'),
    'nothing': ('В доступных фрагментах файла {a} не нашёл ответа на этот вопрос (это не значит, что в документе этого точно нет).',
                'I found no answer in the available fragments of {a} (this does not prove the document lacks it).',
                'In den verfügbaren Abschnitten von {a} habe ich keine Antwort gefunden (das beweist nicht, dass sie fehlt).',
                'Aucune réponse dans les fragments disponibles de {a} (cela ne prouve pas son absence).',
                'No encontré respuesta en los fragmentos disponibles de {a} (eso no demuestra que no esté).',
                'Nessuna risposta nei frammenti disponibili di {a} (non prova che manchi).',
                'Não encontrei resposta nos fragmentos disponíveis de {a} (isso não prova que não exista).'),
    'missing_side': ('Для сравнения не хватает данных: в файле {side} ({name}) не нашлось подходящих фрагментов. Сравнение не составляю.',
                     'Not enough for a comparison: file {side} ({name}) has no matching fragments. I am not making up a comparison.',
                     'Zu wenig für einen Vergleich: In Datei {side} ({name}) gibt es keine passenden Abschnitte.',
                     'Pas assez pour comparer : le fichier {side} ({name}) n’a aucun fragment pertinent.',
                     'No basta para comparar: el archivo {side} ({name}) no tiene fragmentos adecuados.',
                     'Non basta per un confronto: il file {side} ({name}) non ha frammenti pertinenti.',
                     'Não chega para comparar: o ficheiro {side} ({name}) não tem fragmentos adequados.'),
    'stale': ('Внимание: файл {a} изменился или удалён после индексации — ответ основан на проиндексированной версии.',
              'Note: {a} changed or was deleted after indexing — the answer uses the indexed version.',
              'Hinweis: {a} wurde nach der Indizierung geändert oder gelöscht – die Antwort nutzt die indizierte Version.',
              'Attention : {a} a changé ou a été supprimé après l’indexation — la réponse utilise la version indexée.',
              'Aviso: {a} cambió o se borró tras indexarse; la respuesta usa la versión indexada.',
              'Nota: {a} è cambiato o è stato eliminato dopo l’indicizzazione; la risposta usa la versione indicizzata.',
              'Atenção: {a} mudou ou foi apagado depois da indexação; a resposta usa a versão indexada.'),
    'compare_question': ('Сравни документ A и документ B{topic}. Для каждого утверждения укажи, из A оно или из B.',
                         'Compare document A and document B{topic}. Mark each statement as coming from A or B.',
                         'Vergleiche Dokument A und Dokument B{topic}. Kennzeichne jede Aussage mit A oder B.',
                         'Compare le document A et le document B{topic}. Indique pour chaque point s’il vient de A ou de B.',
                         'Compara el documento A y el documento B{topic}. Indica en cada punto si viene de A o de B.',
                         'Confronta il documento A e il documento B{topic}. Indica per ogni punto se viene da A o da B.',
                         'Compara o documento A e o documento B{topic}. Indica em cada ponto se vem de A ou de B.'),
}
_TOPIC = (' по теме «{q}»', ' on “{q}”', ' zum Thema „{q}“', ' sur « {q} »', ' sobre «{q}»', ' su «{q}»', ' sobre «{q}»')


def t(key: str, code: str, **values) -> str:
    return _TEXT[key][_ORDER.index(code) if code in _ORDER else 1].format(**values)


def short_label(path: str, others: list[str] = ()) -> str:
    """The file name plus as much of the parent path as needed to tell files apart."""
    parts = str(path).replace('/', '\\').split('\\')
    for depth in range(1, len(parts) + 1):
        label = '\\'.join(parts[-depth:])
        if not any(o != path and str(o).replace('/', '\\').casefold().endswith(label.casefold()) for o in others):
            return label if depth > 1 or not others else label
    return str(path)


def resolve(index: Any, name: str) -> list[str]:
    """Indexed files that the name denotes: an exact path, or every same-named file."""
    value = _unquote(name).rstrip('\\/')
    if not value:
        return []
    files = [row['path'] for row in index.files()]
    key = lambda p: str(p).replace('/', '\\').casefold()
    exact = [f for f in files if key(f) == key(value)]
    if exact:
        return exact[:1]
    if '\\' in value or '/' in value:
        return [f for f in files if key(f).endswith('\\' + key(value).lstrip('\\'))]
    name_key = value.casefold()
    found = [f for f in files if ntpath.basename(key(f)) == name_key]
    if not found:
        found = [f for f in files if os.path.splitext(ntpath.basename(key(f)))[0] == name_key]
    return found


def stale(index: Any, path: str) -> bool:
    row = next((r for r in index.files() if r['path'] == path), None)
    try:
        info = Path(path).stat()
    except OSError:
        return True
    return row is None or (float(row['mtime'] or 0), int(row['size'] or 0)) != (info.st_mtime, info.st_size)
