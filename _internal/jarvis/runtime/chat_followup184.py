"""Bounded requests to rephrase/explain the previous assistant answer.

These full-string conversational forms never describe a desktop operation.
The caller must require an actual scoped prior answer and no pending task.
"""
from __future__ import annotations
import re

_PATTERNS = {
    'ru': r'(?:скажи (?:то же самое|это) (?:короче|проще)|объясни (?:это )?проще|можешь привести (?:конкретный )?пример|почему ты так считаешь)',
    'en': r'(?:say (?:the same thing|that|it) (?:more briefly|shorter|more simply)|explain (?:that |it )?more simply|can you give (?:a )?(?:specific )?example|why do you think that)',
    'de': r'(?:sag (?:dasselbe|das) (?:kürzer|einfacher)|erklär(?:e)? (?:das |es )?einfacher|kannst du ein (?:konkretes )?beispiel nennen|warum denkst du das)',
    'fr': r'(?:dis (?:la même chose|cela) (?:plus brièvement|plus simplement)|explique (?:cela )?plus simplement|peux-tu donner un exemple(?: concret)?|pourquoi penses-tu cela)',
    'es': r'(?:di (?:lo mismo|eso) (?:más brevemente|más corto|más sencillo)|explíca(?:lo|me) (?:más )?sencillo|puedes dar un ejemplo(?: concreto)?|por qué piensas eso)',
    'it': r'(?:di[’\']? (?:la stessa cosa|questo) (?:più brevemente|più semplicemente)|spiega(?:lo|mi) più semplicemente|puoi fare un esempio(?: concreto)?|perché la pensi così)',
    'pt': r'(?:diz (?:o mesmo|isso) (?:de forma mais breve|mais brevemente|mais simples)|explica (?:isso )?de forma mais simples|podes dar um exemplo(?: concreto)?|porque pensas isso)',
}
_COMPILED = {code: re.compile(pattern, re.I) for code, pattern in _PATTERNS.items()}


def is_followup(text: str, code: str) -> bool:
    pattern = _COMPILED.get(code)
    value = re.sub(r'\s+', ' ', str(text or '')).strip(' \t\r\n¿?!.')
    return bool(pattern and pattern.fullmatch(value))
