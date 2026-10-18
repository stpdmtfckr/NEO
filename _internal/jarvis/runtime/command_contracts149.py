"""Reviewed command contracts, before broad legacy normalization.

Read-only phrases and genuinely incomplete command shells are separate. No
phrase in this module can authorize a new kind of action or bypass the registry.
The test oracle does not call these functions to invent its expected parameters.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell
from jarvis.tools.argument_validation import normalize_arguments

_READONLY = frozenset(['application.list', 'application.list_active', 'timer.list', 'audio.list_sessions', 'audio.list_devices', 'audio.get_input_volume', 'browser.list_browsers', 'media.status', 'music.status', 'music.background_status', 'music.queue_status', 'spotify.status', 'spotify.background_status', 'yandex_music.status', 'yandex_music.background_status', 'yandex_music.api_status', 'yandex_music.queue_status', 'discord.list_channels', 'discord.text_channel_status', 'discord.background_status', 'discord.music_queue_status', 'system_monitor.summary', 'system_monitor.cpu', 'system_monitor.gpu', 'system_monitor.memory', 'system_monitor.disks', 'system_monitor.network', 'downloads.list', 'downloads.status', 'activity.list', 'scenario.list', 'memory.list'])
_MISSING = frozenset({'browser.activate_tab_title', 'music.queue_next', 'music.queue_add'} | {
    f'{tool}.{action}' for tool in ('music','spotify','yandex_music')
    for action in ('search_track','find_track','find_song','play_track','play_song')
})
_TAILS = {
    'ru': r'пожалуйста|будь добр|будь добра', 'en': r'please|if you please',
    'de': r'bitte', 'fr': r"s['’]il te plaît|s['’]il vous plaît",
    'es': r'por favor', 'it': r'per favore|per piacere', 'pt': r'por favor',
}


def polite_tail(text: str, code: str) -> str:
    """Used only for command lookup/missing fields, never for dictated content.

    A colon or quoted suffix may belong to a literal title/message. Do not trim
    it; ordinary templates continue to receive the original text.
    """
    value = str(text).strip()
    if code not in LANGUAGES or ':' in value or value.endswith(('"', "'", '»', '”')):
        return value
    return re.sub(r'[,;]\s*(?:' + _TAILS[code] + r')\s*[.!?]*$', '', value, flags=re.I).strip()


def command_key(text: str, code: str) -> str:
    return norm(polite_tail(strip_request_shell(text, code), code))


@lru_cache(maxsize=1)
def _data() -> dict:
    file = Path(__file__).resolve().parents[2] / 'config/intents/contracts149.json'
    result = json.loads(file.read_text(encoding='utf-8'))
    if result.get('schema_version') != 1:
        raise ValueError('Unsupported command contracts schema')
    return result


@lru_cache(maxsize=14)
def _index(code: str, missing: bool = False) -> dict:
    if code not in LANGUAGES:
        return {}
    kind, allowed = ('incomplete', _MISSING) if missing else ('readonly', _READONLY)
    result = {}
    for intent, row in _data()[kind].get(code, {}).items():
        if intent not in allowed:
            raise ValueError('Unreviewed command contract: ' + intent)
        tool, action = intent.split('.', 1)
        arguments = normalize_arguments(tool, {'action': action})
        phrases = row['phrases'] if missing else row
        binding = (tool, arguments, tuple(row['slot'])) if missing else (tool, arguments)
        for phrase in phrases:
            if '{' in phrase:
                raise ValueError('Literal contract cannot contain a template slot')
            key = command_key(phrase, code)
            previous = result.get(key)
            if previous is not None and previous != binding:
                raise ValueError('Conflicting command contracts: ' + code + ':' + key)
            result[key] = binding
    return result


def match_readonly(text: str, code: str) -> tuple[str, dict] | None:
    found = _index(code).get(command_key(text, code))
    return (found[0], dict(found[1])) if found else None


def missing_contract(text: str, code: str):
    found = _index(code, True).get(command_key(text, code))
    return (found[0], dict(found[1]), found[2]) if found else None


# Explicit track noun required. This does not capture generic app launches,
# video commands, quoted prose, questions about a past action, or an entire
# foreign command. The captured title is copied with its original spelling.
_MUSIC = {
 'ru': (r'найди|найти|поищи|отыщи', r'включи|поставь|проиграй|запусти', r'(?:мне\s+)?(?:песню|песни|трек|композицию)'),
 'en': (r'find|search for|look for|look up', r'play|start', r'(?:(?:a|the)\s+)?(?:song|track)'),
 'de': (r'finde|suche(?: nach)?', r'spiele|starte', r'(?:(?:ein|einen|das|den)\s+)?(?:lied|song|titel|track)'),
 'fr': (r'cherche|chercher|trouve|trouver', r'joue|jouer|lance|lancer|mets', r'(?:(?:un|une|le|la)\s+)?(?:chanson|morceau|titre)'),
 'es': (r'busca|buscar|encuentra', r'pon|reproduce|reproducir', r'(?:(?:una|la|un|el)\s+)?(?:canción|cancion|tema|pista)'),
 'it': (r'cerca|cercare|trova', r'riproduci|riprodurre|metti', r'(?:(?:una|la|un|il)\s+)?(?:canzone|brano|traccia)'),
 'pt': (r'procura|procure|encontra|encontre|pesquisa', r'toca|toque|reproduz|reproduza|põe', r'(?:(?:uma|a|um|o)\s+)?(?:música|musica|canção|cancao|faixa)'),
}
_PROVIDER = r'(?:spotify|спотифай|спотифи|yandex(?:\s+music)?|яндекс(?:\s+музык[аеиуы])?)'
_PREP = r'(?:в|на|через|in|on|from|using|bei|auf|sur|dans|en|de|su|da|no|na|em)'


@lru_cache(maxsize=7)
def _music_patterns(code: str):
    search, play, noun = _MUSIC[code]
    return (re.compile(r'^(?P<verb>' + search + '|' + play + r')\s+' + noun + r'(?:\s+(?P<rest>.*))?$', re.I),
            re.compile(r'^(?:' + search + r')$', re.I),
            re.compile(r'(?:^|\s)' + _PREP + r'\s+(?P<provider>' + _PROVIDER + r')\s*$', re.I))


def parse_music_request(text: str, code: str) -> tuple[str, dict] | None:
    if code not in LANGUAGES:
        return None
    value = polite_tail(strip_request_shell(text, code), code).strip(' .!?¿¡')
    pattern, searching, provider_pattern = _music_patterns(code)
    match = pattern.fullmatch(value)
    if not match:
        return None
    rest = (match['rest'] or '').strip()
    if re.search(r'\b(?:discord|дискорд|youtube|ютуб|очеред\w*|queue|warteschlange|cola|coda|fila)\b', rest, re.I):
        return None  # these explicit owners have their own queue/video grammar
    if re.search(r'https?://', rest, re.I):
        return None  # URL ownership belongs to existing video/URL handlers.
    provider = 'music'
    suffix = provider_pattern.search(rest)
    if suffix:
        provider = 'spotify' if re.search(r'spotify|споти', suffix['provider'], re.I) else 'yandex_music'
        rest = rest[:suffix.start()].strip()
    action = 'search_track' if searching.fullmatch(match['verb']) else 'play_track'
    arguments = {'action': action}
    if rest:
        # Only balanced outer quotes are presentation syntax.
        if len(rest) > 1 and (rest[0],rest[-1]) in {('"','"'),('«','»'),('“','”')}:
            rest = rest[1:-1]
        arguments['query'] = rest
    return provider, arguments


# Exact native transport phrases must outrank the generic "play song <title>".
# This is a bounded declaration, not a guess based on a substring or score.
_LITERAL_TRANSPORT = frozenset(
    f'{tool}.{action}' for tool in ('music','spotify','yandex_music')
    for action in ('play','pause','stop','next','previous','play_pause','shuffle_on','shuffle_off',
                   'repeat_track','repeat_on','repeat_off','repeat_playlist','volume_up','volume_down',
                   'like','unlike','dislike')) | frozenset({
    'music.my_wave','music.my_tracks','yandex_music.my_wave','yandex_music.my_tracks',
    'system.open_settings','desktop_features.settings','briefing.summary'})

@lru_cache(maxsize=7)
def _transport_index(code: str):
    if code not in LANGUAGES: return {}
    root = Path(__file__).resolve().parents[2] / 'config'
    rows = []
    for path in sorted((root/'canonical_phrases').glob('*.json')):
        for intent, phrases in json.loads(path.read_text(encoding='utf-8')).items():
            if intent in _LITERAL_TRANSPORT: rows.append((intent,phrases[LANGUAGES.index(code)]))
    for path in sorted((root/'command_variants').glob('*.json')):
        for intent, languages in json.loads(path.read_text(encoding='utf-8')).items():
            if intent in _LITERAL_TRANSPORT:
                rows.extend((intent,phrase) for phrase in languages.get(code,[]))
    pack_path = root.parent / 'jarvis/locales/commands' / (code + '.json')
    pack = json.loads(pack_path.read_text(encoding='utf-8'))
    for intent, phrases in pack.get('action_phrases', {}).items():
        if intent in _LITERAL_TRANSPORT:
            rows.extend((intent,phrase) for phrase in phrases if '{' not in phrase)
    result = {}; ambiguous = set()
    for intent, phrase in rows:
        if '{' in phrase: continue
        tool, action = intent.split('.',1)
        binding = (tool,normalize_arguments(tool,{'action':action}))
        key = command_key(phrase,code)
        if key in result and result[key] != binding:
            ambiguous.add(key)
        result[key] = binding
    # Conflicting catalogue labels are not permission to choose an action.
    for key in ambiguous: result.pop(key,None)
    return result

def match_literal_transport(text: str, code: str):
    found = _transport_index(code).get(command_key(text,code))
    return (found[0],dict(found[1])) if found else None
