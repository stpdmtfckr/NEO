"""NEO 179: strict «стоп» while an answer is being written, and the note shown after it.

(Moved out of the removed speech-output package: NEO no longer speaks, but a
running answer or task can still be stopped by voice.) Never interprets a
substring as Stop.
"""
from __future__ import annotations

import re
import unicodedata

_STOP = {
    'ru': ('стоп', 'стап', 'остановись', 'хватит', 'останови ответ', 'прекрати ответ', 'не продолжай'),
    'en': ('stop', 'stop the answer', 'cancel response'),
    'de': ('stopp', 'stop', 'hör auf', 'antwort stoppen'),
    'fr': ('stop', 'arrête', 'arrête la réponse'),
    'es': ('stop', 'alto', 'para', 'detén la respuesta'),
    'it': ('stop', 'fermati', 'interrompi la risposta'),
    'pt': ('stop', 'para', 'pare', 'interrompe a resposta'),
}
STOPPED = dict(zip(_STOP, (
    'Ответ остановлен. Показанный текст сохранён; уже выполненные действия не отменены.',
    'Answer stopped. The visible text is kept; completed actions have not been undone.',
    'Antwort gestoppt. Sichtbarer Text bleibt erhalten; ausgeführte Aktionen wurden nicht rückgängig gemacht.',
    'Réponse interrompue. Le texte visible est conservé ; les actions déjà réalisées ne sont pas annulées.',
    'Respuesta detenida. El texto visible se conserva; las acciones realizadas no se han deshecho.',
    'Risposta interrotta. Il testo visibile è conservato; le azioni completate non sono state annullate.',
    'Resposta interrompida. O texto visível foi mantido; as ações concluídas não foram desfeitas.',
)))


def _norm(text: str) -> str:
    value = unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')
    return re.sub(r'[^\w\s-]', ' ', value).strip().replace('-', ' ').strip()


def stop_phrase(text: str, language: str = 'ru', addresses=()) -> bool:
    if any(x in str(text) for x in ('«', '»', '"', '“', '”')):
        return False
    value = re.sub(r'\s+', ' ', _norm(text))
    # Remove only a prefix address, not a name or word anywhere in a sentence.
    for address in sorted((re.sub(r'\s+', ' ', _norm(a)) for a in addresses if a), key=len, reverse=True):
        if value.startswith(address + ' '):
            value = value[len(address):].strip()
            break
    choices = {_norm(x) for x in _STOP.get(language, _STOP['en'])} | {'stop'}
    if value in choices:
        return True
    words = value.split()
    return 1 < len(words) <= 4 and all(w in choices for w in words)


__all__ = ['STOPPED', 'stop_phrase']
