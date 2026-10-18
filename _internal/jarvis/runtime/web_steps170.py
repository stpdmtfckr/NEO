"""NEO 170: «загугли …» as a step of a task means a Google search in the browser.

On its own, «загугли X» keeps its existing meaning (a web answer when SearchAPI is
configured). Inside a sequence («открой браузер, загугли X и перейди по первой
ссылке») the user describes what to do in the browser, so the step opens the
Google results page. Only the search words are recognised here; the query is kept
verbatim.
"""
from __future__ import annotations

import re
from typing import Any

_SEARCH = {
    'ru': [r'(?:загугли|погугли|гугли|пробей\s+в\s+гугле)\s+(?P<q>.+)',
           r'(?:найди|поищи|ищи)\s+(?:в\s+|через\s+)(?:гугле|гугл|google|интернете|сети|браузере|яндексе)\s+(?P<q>.+)',
           r'(?:найди|поищи)\s+(?P<q>.+?)\s+(?:в\s+|через\s+)(?:гугле|гугл|google|интернете|сети|браузере)'],
    'en': [r'(?:google|search\s+google\s+for|search\s+the\s+web\s+for|search\s+for|look\s+up)\s+(?P<q>.+?)(?:\s+(?:on|in)\s+(?:google|the\s+web|the\s+browser))?',
           r'(?:search|look)\s+(?:on\s+|in\s+)?(?:google|the\s+web|the\s+browser)\s+for\s+(?P<q>.+)'],
    'de': [r'(?:google|googel|such(?:e)?\s+(?:bei|in|mit)\s+google\s+nach|such(?:e)?\s+im\s+(?:internet|web|browser)\s+nach)\s+(?P<q>.+)',
           r'such(?:e)?\s+(?P<q>.+?)\s+(?:bei|in)\s+google'],
    'fr': [r'(?:google|googlise|cherche\s+sur\s+(?:google|internet|le\s+web)|recherche\s+sur\s+(?:google|internet))\s+(?P<q>.+)',
           r'(?:cherche|recherche)\s+(?P<q>.+?)\s+sur\s+(?:google|internet|le\s+web)'],
    'es': [r'(?:googlea|guglea|busca\s+en\s+(?:google|internet|la\s+web))\s+(?P<q>.+)',
           r'busca\s+(?P<q>.+?)\s+en\s+(?:google|internet|la\s+web)'],
    'it': [r'(?:googla|cerca\s+su\s+(?:google|internet))\s+(?P<q>.+)',
           r'cerca\s+(?P<q>.+?)\s+su\s+(?:google|internet)'],
    'pt': [r'(?:googla|pesquisa\s+no\s+(?:google|navegador)|procura\s+no\s+google|pesquisa\s+na\s+internet)\s+(?P<q>.+)',
           r'(?:pesquisa|procura)\s+(?P<q>.+?)\s+(?:no\s+google|na\s+internet)'],
}
_COMPILED = {code: [re.compile(r'^\s*' + p + r'\s*[.!?]?\s*$', re.I) for p in patterns] for code, patterns in _SEARCH.items()}
# Words that start a search step (used to decide where a sequence may be split).
SEARCH_VERBS = {
    'ru': {'загугли', 'погугли', 'гугли', 'найди', 'поищи', 'ищи', 'пробей'},
    'en': {'google', 'search', 'look'},
    'de': {'google', 'googel', 'such', 'suche'},
    'fr': {'google', 'googlise', 'cherche', 'recherche'},
    'es': {'googlea', 'guglea', 'busca'},
    'it': {'googla', 'cerca'},
    'pt': {'googla', 'pesquisa', 'procura'},
}
_BROWSERS = {'browser', 'браузер', 'chrome', 'хром', 'google chrome', 'edge', 'microsoft edge', 'firefox', 'файрфокс', 'opera', 'опера',
             'yandex browser', 'яндекс браузер', 'яндекс.браузер', 'brave', 'navigateur', 'navegador', 'browser web'}


def search_step(text: str, code: str) -> tuple[str, dict[str, Any]] | None:
    for pattern in _COMPILED.get(code, ()):
        found = pattern.match(str(text or ''))
        if found:
            query = found.group('q').strip(' «»"“”.,')
            if query:
                return 'browser', {'action': 'search', 'value': query}
    return None


def is_browser_open(tool: str, arguments: dict[str, Any]) -> bool:
    """«открой браузер» / «запусти Chrome» — redundant right before a search or a URL."""
    action = str(arguments.get('action', ''))
    if tool == 'browser' and action in {'open', 'new_tab'} and not str(arguments.get('value', '') or '').strip():
        return True
    if tool == 'application' and action == 'open':
        return str(arguments.get('name', '') or '').strip().casefold() in _BROWSERS
    return False
