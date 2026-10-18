"""NEO 188 NEO-PRIVACY-001: «больше не ищи в папке X» revokes access — never the opposite.

A negated search request about an allowed folder was offered as its opposite
(«ищи в папке …?»). These complete phrases in seven languages name the
revocation explicitly; the folder name stays literal data and the knowledge
tool resolves it to exactly one granted full path (or asks which one).
"""
from __future__ import annotations

import re
from functools import lru_cache

from jarvis.runtime.natural_commands import LANGUAGES, strip_request_shell

_FOLDER = {'ru': r'(?:в\s+)?(?:папк[еиу]|каталог[еау]?)', 'en': r'(?:in\s+|to\s+)?(?:the\s+)?(?:folder|directory)',
           'de': r'(?:im|in\s+dem|zum|auf\s+den|den|dem)?\s*ordner', 'fr': r'(?:dans\s+|au\s+)?(?:le\s+)?dossier',
           'es': r'(?:en\s+|a\s+)?(?:la\s+)?carpeta', 'it': r'(?:nella|alla|la)?\s*cartella', 'pt': r'(?:na|à|a)?\s*pasta'}
_REVOKE = {
    'ru': (r'больше\s+не\s+ищи', r'не\s+ищи\s+больше', r'запрети\s+(?:искать|поиск)', r'перестань\s+искать',
           r'отзови\s+доступ\s+к', r'закрой\s+доступ\s+к', r'убери\s+доступ\s+к', r'забудь\s+(?:про|о)'),
    'en': (r'stop\s+searching', r"don'?t\s+search\s+anymore", r'do\s+not\s+search\s+anymore', r'no\s+longer\s+search',
           r'revoke\s+(?:the\s+)?access\s+to', r'remove\s+(?:the\s+)?access\s+to', r'forget'),
    'de': (r'such(?:e)?\s+nicht\s+mehr', r'durchsuche\s+nicht\s+mehr', r'hör\s+auf\s+zu\s+suchen', r'entzieh(?:e)?\s+den\s+zugriff\s+auf', r'widerrufe?\s+den\s+zugriff\s+auf'),
    'fr': (r'ne\s+cherche\s+plus', r'arrête\s+de\s+chercher', r'retire\s+l[’\']accès\s+(?:à|au)', r'révoque\s+l[’\']accès\s+(?:à|au)'),
    'es': (r'no\s+busques\s+más', r'deja\s+de\s+buscar', r'quita\s+el\s+acceso\s+a', r'revoca\s+el\s+acceso\s+a'),
    'it': (r'non\s+cercare\s+più', r'smetti\s+di\s+cercare', r'togli\s+l[’\']accesso\s+(?:a|alla)', r'revoca\s+l[’\']accesso\s+(?:a|alla)'),
    'pt': (r'não\s+procures\s+mais', r'deixa\s+de\s+procurar', r'para\s+de\s+procurar', r'retira\s+o\s+acesso\s+(?:a|à)', r'revoga\s+o\s+acesso\s+(?:a|à)'),
}


@lru_cache(maxsize=7)
def _pattern(code: str):
    verbs = '|'.join(_REVOKE[code])
    return re.compile(r'^(?:' + verbs + r')\s+' + _FOLDER[code] + r'\s+(?P<folder>.+?)$', re.I)


def parse(text: str, code: str) -> tuple[str, dict] | None:
    if code not in LANGUAGES:
        return None
    value = strip_request_shell(str(text or ''), code).strip(' .!?¿¡')
    match = _pattern(code).fullmatch(value)
    if not match:
        return None
    folder = match.group('folder').strip().strip('"«»“”')
    if not folder or len(folder) > 260:
        return None
    return 'knowledge', {'action': 'remove_folder', 'folder': folder}
