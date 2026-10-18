"""Reviewed surface repairs; arbitrary payload words are never spell-checked."""
import re

WAKE_NAMES = ('нео', 'нио', 'нэо', 'нейо', 'neo', 'nio', 'néo', 'джарвис', 'джарвес', 'жарвис', 'жарвес', 'jarvis', 'jarves', 'jervis', 'djarvis')
GREETINGS = r"(?:привет|здравствуй|эй|hello|hi|hey|hallo|bonjour|salut|hola|ciao|olá|oi)"
ACTIONS = frozenset(('открой открыть запусти запустить включи выключи поставь покажи продолжи '
                     'приостанови останови закрой удали найди создай открой '
                     'open launch run start close show play pause stop resume find '
                     'öffne starte zeige spiele pausiere stoppe '
                     'ouvre lance montre joue reprends ferme '
                     'abre inicia muestra reproduce reanuda cierra '
                     'apri avvia mostra riproduci riprendi chiudi '
                     'abra inicie mostre reproduza retome feche').split())
TYPO_VERBS = {'аткрой':'открой', 'открои':'открой', 'вклучи':'включи',
              'пакажи':'покажи', 'прадолжи':'продолжи'}
FILLERS = re.compile(r"^(?:ну|эм|э|пожалуйста|можешь|можно|please|could you|can you|bitte|por favor|per favore|s'il te plaît)\s+", re.I)


def normalize_spoken(text, *, wake='NEO'):
    value = str(text or '').strip()
    aliases = tuple(dict.fromkeys((wake, *WAKE_NAMES)))
    pattern = re.compile(r"^\s*(?:" + GREETINGS + r"[\s,!.:;—-]+)?(?:" +
                         '|'.join(re.escape(x) for x in aliases if x) + r")(?!\w)[\s,!.?:;—-]*", re.I)
    for _ in range(6):
        new = pattern.sub('', value, count=1)
        if new == value:
            break
        value = new
    # Drop polite lead-ins only when an actual command follows them.
    candidate = value
    for _ in range(3):
        candidate = FILLERS.sub('', candidate, count=1)
    first = re.match(r"[^\W_]+", candidate)
    if first and (first[0].casefold() in ACTIONS or first[0].casefold() in TYPO_VERBS):
        value = candidate
    first = re.match(r"[^\W_]+", value)
    if first and first[0].casefold() in TYPO_VERBS:
        value = TYPO_VERBS[first[0].casefold()] + value[first.end():]
    # Only repeated leading action words, never names, lyrics or message bodies.
    for _ in range(3):
        match = re.match(r"([^\W_]+)[\s,;—-]+([^\W_]+)\b", value, re.I)
        if not match or match[2].casefold() not in ACTIONS:
            break
        a, b = match[1].casefold(), match[2].casefold()
        if a != b and not (len(a) >= 3 and b.startswith(a)):
            break
        value = value[match.start(2):]
    return value.strip()
