"""NEO 177: NEO remembers what a misspelled request meant and answers it at once next time.

* «привте» once took the long way (every route declined it, then the model guessed);
  now a short phrase with a typo is first compared with NEO's conversation phrases
  (``social_repair``): «привте, как дила» -> «привет, как дела» in a few milliseconds.
* When a typo repair or the agent model resolved a short request and the answer
  worked, the pair is kept (``remember``). The next identical request is replaced
  before routing (``lookup``) and goes straight to the right answer or command,
  with the ordinary checks and confirmations.
* Only short requests (up to 6 words) that resolve to a conversation phrase or to a
  NEO command; never messages, notes, search text or other free text, never a command
  that needs a confirmation, never «это / туда / его», whose meaning depends on the
  moment. «не то» right after such an answer forgets the pair; «удали собранные фразы»
  forgets all of them; Settings → «Запоминать понятые опечатки» turns it off.
* Stored locally in ``<NeoJarvis>/learned_phrases.json`` (removed with «История чата и
  память» at uninstall), at most ``LIMIT`` phrases per language, the least used go first.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

LIMIT = 800
MAX_WORDS = 6
MAX_CHARS = 80
FILE_NAME = 'learned_phrases.json'
# Arguments that carry the user's own text: never learned.
PAYLOAD_FIELDS = frozenset({'text', 'body', 'message', 'content', 'note', 'title', 'prompt', 'query', 'subject', 'reply',
                            'question', 'search', 'phrase', 'value', 'url', 'path', 'recipient', 'to', 'contact',
                            'values', 'items', 'topic', 'find'})
# Words whose meaning depends on the moment («закрой его»): such a phrase is never learned.
_CONTEXT_WORDS = {
    'ru': {'это', 'этот', 'эта', 'эту', 'этого', 'этой', 'его', 'ее', 'её', 'их', 'него', 'нее', 'неё', 'них', 'туда', 'тут',
           'там', 'здесь', 'тот', 'та', 'ту', 'того', 'той', 'те', 'тех', 'оттуда', 'отсюда', 'сюда', 'такой', 'такое',
           'предыдущий', 'предыдущую', 'прошлый', 'прошлую', 'последний', 'последнюю', 'следующий', 'следующую', 'снова', 'опять',
           'ещё', 'еще', 'сейчас', 'сегодня', 'завтра', 'вчера'},
    'en': {'it', 'this', 'that', 'these', 'those', 'there', 'here', 'them', 'him', 'her', 'previous', 'last', 'next', 'again',
           'same', 'now', 'today', 'tomorrow', 'yesterday'},
    'de': {'es', 'das', 'dies', 'diese', 'diesen', 'dieses', 'dort', 'hier', 'dahin', 'ihn', 'sie', 'ihm', 'vorherige', 'letzte',
           'nächste', 'nochmal', 'wieder', 'jetzt', 'heute', 'morgen', 'gestern'},
    'fr': {'ça', 'ca', 'cela', 'ceci', 'ce', 'cet', 'cette', 'là', 'ici', 'lui', 'précédent', 'dernier',
           'suivant', 'encore', 'maintenant', "aujourd'hui", 'demain', 'hier'},
    'es': {'esto', 'eso', 'este', 'esta', 'ese', 'esa', 'ahí', 'allí', 'aquí', 'anterior', 'último',
           'siguiente', 'otra', 'ahora', 'hoy', 'mañana', 'ayer'},
    'it': {'questo', 'questa', 'quello', 'quella', 'lì', 'qui', 'là', 'qua', 'precedente', 'ultimo',
           'prossimo', 'ancora', 'adesso', 'ora', 'oggi', 'domani', 'ieri'},
    'pt': {'isto', 'isso', 'aquilo', 'este', 'esta', 'esse', 'essa', 'aí', 'ali', 'aqui', 'lá', 'anterior',
           'último', 'próximo', 'outra', 'agora', 'hoje', 'amanhã', 'ontem'},
}
_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.U)
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def enabled(config: Any) -> bool:
    try:
        return bool(config.get('learning.remember_resolved', True))
    except Exception:
        return False


def norm(text: str) -> str:
    """The lookup key: case, punctuation, «ё» and extra spaces do not matter."""
    return ' '.join(_TOKEN.findall(str(text or '').casefold().replace('ё', 'е')))


def _path(config: Any) -> Path | None:
    root = getattr(getattr(config, 'paths', None), 'root', None)
    return Path(root) / FILE_NAME if root else None


def _load(path: Path) -> dict[str, dict[str, Any]]:
    key = str(path)
    try:
        stamp = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        stamp = 0.0
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    data: dict[str, dict[str, Any]] = {}
    if stamp:
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(raw, dict) and isinstance(raw.get('languages'), dict):
                data = {str(code): dict(rows) for code, rows in raw['languages'].items() if isinstance(rows, dict)}
        except (OSError, ValueError):
            logger.debug('Learned phrases unreadable; starting empty', exc_info=True)
    _CACHE[key] = (stamp, data)
    return data


def _save(path: Path, data: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 1, 'languages': data}, ensure_ascii=False, indent=1), encoding='utf-8')
    temporary.replace(path)
    try:
        _CACHE[str(path)] = (path.stat().st_mtime, data)
    except OSError:
        _CACHE.pop(str(path), None)


def eligible(text: str, code: str) -> bool:
    """A short request whose meaning does not depend on the moment."""
    value = str(text or '').strip()
    words = norm(value).split()
    if not words or len(words) > MAX_WORDS or len(value) > MAX_CHARS:
        return False
    if any(mark in value for mark in ('"', '«', '»', '“', '”', ':', '/', '\\', '@', 'http')):
        return False
    return not (set(words) & _CONTEXT_WORDS.get(code, set()))


def lookup(config: Any, text: str, code: str) -> str:
    """The remembered meaning of exactly this request, or ''."""
    path = _path(config)
    key = norm(text)
    if path is None or not key or not enabled(config):
        return ''
    with _LOCK:
        row = _load(path).get(code, {}).get(key)
    return str(row.get('text', '') or '') if isinstance(row, dict) else ''


def remember(config: Any, original: str, resolved: str, code: str, kind: str) -> bool:
    """Keep «original means resolved» (only short, context-free requests)."""
    path = _path(config)
    key = norm(original)
    resolved = str(resolved or '').strip()
    if path is None or not enabled(config) or not key or not resolved or key == norm(resolved):
        return False
    if not eligible(original, code) or len(resolved) > 160:
        return False
    with _LOCK:
        data = _load(path)
        rows = dict(data.get(code, {}))
        previous = rows.get(key) or {}
        rows[key] = {'text': resolved, 'kind': str(kind or ''), 'uses': int(previous.get('uses', 0) or 0),
                     'at': round(time.time())}
        if len(rows) > LIMIT:
            ranked = sorted(rows.items(), key=lambda item: (int(item[1].get('uses', 0) or 0), int(item[1].get('at', 0) or 0)))
            for name, _row in ranked[:len(rows) - LIMIT]:
                rows.pop(name, None)
        data = {**data, code: rows}
        try:
            _save(path, data)
        except OSError:
            logger.debug('Learned phrase was not saved', exc_info=True)
            return False
    logger.info('Learned: %r -> %r (%s)', key[:60], resolved[:60], kind)
    return True


def used(config: Any, text: str, code: str) -> None:
    """One more use of a remembered phrase (the least used are dropped first)."""
    path = _path(config)
    key = norm(text)
    if path is None or not key:
        return
    with _LOCK:
        data = _load(path)
        row = data.get(code, {}).get(key)
        if not isinstance(row, dict):
            return
        rows = dict(data.get(code, {}))
        rows[key] = {**row, 'uses': int(row.get('uses', 0) or 0) + 1, 'at': round(time.time())}
        try:
            _save(path, {**data, code: rows})
        except OSError:
            logger.debug('Learned phrase use was not saved', exc_info=True)


def forget(config: Any, text: str, code: str) -> bool:
    path = _path(config)
    key = norm(text)
    if path is None or not key:
        return False
    with _LOCK:
        data = _load(path)
        rows = dict(data.get(code, {}))
        if rows.pop(key, None) is None:
            return False
        try:
            _save(path, {**data, code: rows})
        except OSError:
            return False
    logger.info('Forgot learned phrase %r', key[:60])
    return True


def clear(config: Any) -> int:
    path = _path(config)
    if path is None:
        return 0
    with _LOCK:
        count = sum(len(rows) for rows in _load(path).values())
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return 0
        _CACHE.pop(str(path), None)
    return count


def count(config: Any) -> int:
    path = _path(config)
    if path is None:
        return 0
    with _LOCK:
        return sum(len(rows) for rows in _load(path).values())


# ---- conversation phrases with a typo ---------------------------------------------------------

def _budget(word: str) -> int:
    """How many letters may differ in one word: none in short words («кто» is not «что»)."""
    return 0 if len(word) <= 3 else 1 if len(word) <= 6 else 2


def _known(word: str, code: str) -> bool:
    try:
        from jarvis.runtime.spoken_spelling165 import is_known
        return is_known(word, code)
    except Exception:
        return True


def social_repair(text: str, code: str) -> str:
    """The conversation phrase a short typo meant («привте как дила» -> «привет как дела»), or ''.

    Every word that differs must be unknown to the dictionary («кто ты» never becomes
    «что ты») and within one or two letters of the phrase's word; the best phrase must be
    clearly better than any phrase of another reply family.
    """
    from jarvis.runtime import social_atlas
    from jarvis.runtime.typos175 import _distance
    words = norm(text).split()
    if not words or len(words) > MAX_WORDS:
        return ''
    try:
        catalog = social_atlas.catalog(code)
    except Exception:
        return ''
    if catalog.get(social_atlas.norm(text)) or catalog.get(' '.join(words)):
        return ''
    unknown: dict[str, bool] = {}
    scored: list[tuple[int, str, str]] = []
    for phrase, (_category, family) in catalog.items():
        target = norm(phrase).split()
        if len(target) != len(words):
            continue
        total = 0
        for said, meant in zip(words, target):
            if said == meant:
                continue
            limit = _budget(meant)
            if not limit and len(meant) == 3 and sorted(said) == sorted(meant):
                limit = 1                      # «yuo» -> «you»: two letters swapped, nothing else
            if not limit or abs(len(said) - len(meant)) > limit:
                break
            if said not in unknown:
                unknown[said] = not _known(said, code)
            if not unknown[said]:
                break
            cost = _distance(said, meant)
            if cost > limit:
                break
            total += cost
        else:
            if total:
                scored.append((total, phrase, family))
    if not scored:
        return ''
    scored.sort()
    best = scored[0]
    if any(score == best[0] and family != best[2] for score, _phrase, family in scored[1:]):
        return ''
    return best[1]


__all__ = ['LIMIT', 'clear', 'count', 'eligible', 'enabled', 'forget', 'lookup', 'norm', 'remember', 'social_repair', 'used']
