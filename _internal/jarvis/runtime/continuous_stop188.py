"""NEO 188 NEO-VOICE-001 / TZ-CONTINUOUS-DIALOGUE-LOCALIZED-STOP-01.

Ending a continuous voice dialogue in the SELECTED language: «стоп», «stopp»,
«pare», «arrête», «basta», «end the conversation»… The whole utterance must be
one of these bounded forms (optionally with the assistant's name); words of
other languages are not caught globally, and anything naming a target
(«стоп музыка», «stop the video») stays an ordinary media command. The same
check serves the fast and the final recognition paths.
"""
from __future__ import annotations

import re
import unicodedata

_STOP = {
    'ru': {'стоп', 'стап', 'стопп', 'стопэ', 'хватит', 'хватеть', 'перестань слушать', 'перестань меня слушать', 'не слушай',
           'заверши диалог', 'закрой диалог', 'закончи диалог', 'заверши разговор', 'закончи разговор', 'жди wake word',
           'жди вейк ворд', 'ждать wake word', 'конец диалога', 'всё хватит', 'все хватит', 'достаточно',
           'закончи голосовой диалог', 'заверши голосовой диалог', 'останови голосовой диалог'},
    'en': {'stop', 'enough', 'stop listening', 'stop conversation', 'end conversation', 'end the conversation', "that's all",
           'thats all', 'stop the conversation', 'quit listening', 'end the voice conversation',
           'stop the voice conversation'},
    'de': {'stopp', 'stop', 'genug', 'hör auf', 'hör auf zuzuhören', 'hör auf zu hören', 'gespräch beenden', 'beende das gespräch',
           'das reicht', 'schluss', 'beende das sprachgespräch'},
    'fr': {'stop', 'arrête', 'arrête toi', "arrête d'écouter", 'arrête d écouter', 'ça suffit', 'termine la conversation',
           'fin de la conversation', 'arrête la conversation', 'termine la conversation vocale'},
    'es': {'para', 'stop', 'basta', 'detente', 'deja de escuchar', 'termina la conversación', 'fin de la conversación', 'ya basta',
           'termina la conversación de voz'},
    'it': {'stop', 'basta', 'fermati', 'smetti di ascoltare', 'termina la conversazione', 'fine conversazione', 'fine della conversazione', 'termina la conversazione vocale'},
    'pt': {'pare', 'para', 'stop', 'chega', 'para de ouvir', 'pare de ouvir', 'termina a conversa', 'fim da conversa', 'acabou', 'termina a conversa de voz'},
}
_BARE_STOP = {'ru': {'стоп', 'хватит'}, 'en': {'stop', 'enough'}, 'de': {'stopp', 'stop', 'genug'}, 'fr': {'stop', 'arrête'},
              'es': {'para', 'stop', 'basta'}, 'it': {'stop', 'basta'}, 'pt': {'pare', 'para', 'stop', 'chega'}}


def norm(text: str) -> str:
    """Case, punctuation and spacing only; Unicode letters (é, ü, ç, ё) are kept."""
    value = unicodedata.normalize('NFKC', str(text or '')).casefold().replace('ё', 'е').replace('’', "'")
    value = re.sub(r"[^\w']+", ' ', value)
    return ' '.join(value.split())


def _plain(value: str) -> str:
    return ''.join(ch for ch in unicodedata.normalize('NFD', value) if unicodedata.category(ch) != 'Mn')


def is_stop(text: str, code: str, *, configured=(), assistant_words=()) -> bool:
    value = norm(text)
    if not value:
        return False
    forms = {norm(x) for x in _STOP.get(code, _STOP['en'])} | {norm(x) for x in configured if str(x).strip()}
    forms.discard('')
    plain_forms = {_plain(f) for f in forms}
    if value in forms or _plain(value) in plain_forms:
        return True
    names = {norm(x) for x in assistant_words if str(x).strip()}
    names.discard('')
    words = value.split()
    bare = {norm(x) for x in _BARE_STOP.get(code, _BARE_STOP['en'])}
    # «стоп нео», «нео стоп», «нео, перестань слушать»: only the name plus a stop form.
    for name in names:
        name_words = name.split()
        n = len(name_words)
        if words[:n] == name_words and (' '.join(words[n:]) in forms):
            return True
        if words[-n:] == name_words and (' '.join(words[:-n]) in bare or ' '.join(words[:-n]) in forms):
            return True
    return False


_ENDED = {'ru': 'Хорошо. Снова жду wake word.', 'en': 'Okay. Waiting for the wake word again.',
          'de': 'Okay. Ich warte wieder auf das Aktivierungswort.', 'fr': 'D’accord. J’attends de nouveau le mot d’activation.',
          'es': 'De acuerdo. Vuelvo a esperar la palabra de activación.', 'it': 'Va bene. Attendo di nuovo la parola di attivazione.',
          'pt': 'Está bem. Volto a esperar a palavra de ativação.'}


def ended_text(code: str) -> str:
    """Only the listening mode changed — no claim about music, speech or commands."""
    return _ENDED.get(code, _ENDED['en'])
