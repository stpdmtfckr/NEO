"""NEO 187 HREQ-014: one reading of short yes/no replies in seven languages.

«да да», «да, верно», «это то, что я имел в виду» and their equivalents are
consent. Negation, cancellation and mixed replies («да нет», «не знаю») are
never consent. Only a complete short reply is classified; anything else
returns None so the caller keeps its own, narrower rules (an existing
«установи», «ищи» or «найди» answer still works exactly as before).
"""
from __future__ import annotations

import re
from functools import lru_cache

YES, NO, UNCLEAR = "yes", "no", "unclear"

_YES = {
    "ru": ("да", "ага", "угу", "верно", "да верно", "все верно", "да все верно", "правильно", "да правильно",
           "именно", "именно так", "да именно", "именно это", "да именно это", "точно", "да точно", "так точно",
           "конечно", "да конечно", "подтверждаю", "да подтверждаю", "да это", "да это оно", "оно самое",
           "это оно", "то самое", "да то самое", "это то что я имел в виду", "это то что я имела в виду",
           "то что я имел в виду", "то что я имела в виду", "да это то что я имел в виду",
           "да это то что я имела в виду", "я это и имел в виду", "я это и имела в виду", "это то что нужно",
           "то что нужно", "да это то что нужно", "совершенно верно", "абсолютно верно", "да да верно"),
    "en": ("yes", "yeah", "yep", "yup", "correct", "that's correct", "thats correct", "that is correct", "right",
           "that's right", "thats right", "that is right", "yes that's right", "yes thats right", "yes right",
           "yes correct", "exactly", "yes exactly", "that's it", "thats it", "yes that's it", "yes thats it",
           "that's what i meant", "thats what i meant", "that is what i meant", "yes that's what i meant",
           "yes thats what i meant", "that's the one", "thats the one", "confirm", "i confirm", "sure", "yes sure"),
    "de": ("ja", "genau", "ja genau", "richtig", "ja richtig", "stimmt", "ja stimmt", "das stimmt", "korrekt",
           "das meinte ich", "genau das meinte ich", "ja das meinte ich", "das ist es", "ja das ist es",
           "das habe ich gemeint", "genau das", "bestätigen", "ich bestätige", "ja bitte"),
    "fr": ("oui", "exact", "exactement", "c'est ça", "c'est ca", "oui c'est ça", "oui c'est ca", "c'est bien ça",
           "c'est bien ca", "voilà", "voila", "tout à fait", "tout a fait", "oui exact", "oui exactement",
           "c'est ce que je voulais dire", "oui c'est ce que je voulais dire", "confirmer", "je confirme",
           "confirme", "d'accord", "oui d'accord"),
    "es": ("sí", "si", "correcto", "sí correcto", "si correcto", "exacto", "sí exacto", "si exacto", "eso es",
           "sí eso es", "si eso es", "eso mismo", "justo eso", "es lo que quería decir", "es lo que queria decir",
           "eso es lo que quería decir", "eso es lo que queria decir", "a eso me refería", "a eso me referia",
           "confirmo", "sí confirmo", "si confirmo", "claro", "sí claro", "si claro"),
    "it": ("sì", "si", "esatto", "sì esatto", "si esatto", "giusto", "sì giusto", "si giusto", "proprio così",
           "proprio cosi", "è quello che intendevo", "e quello che intendevo", "sì è quello che intendevo",
           "si è quello che intendevo", "intendevo proprio questo", "confermo", "sì confermo", "si confermo",
           "certo", "sì certo", "si certo", "corretto"),
    "pt": ("sim", "certo", "sim certo", "exato", "exatamente", "sim exato", "isso", "isso mesmo", "sim isso mesmo",
           "é isso", "e isso", "sim é isso", "sim e isso", "correto", "sim correto", "era isso que eu queria dizer",
           "é o que eu queria dizer", "e o que eu queria dizer", "era isso", "confirmo", "sim confirmo"),
}
_NO = {
    "ru": ("нет", "неа", "не", "не то", "нет не то", "это не то", "не это", "нет не это", "не надо", "нет не надо",
           "не нужно", "нет не нужно", "отмена", "отмени", "нет отмена", "неверно", "неправильно", "нет неверно",
           "это не то что я имел в виду", "это не то что я имела в виду", "не то что я имел в виду",
           "не то что я имела в виду", "я не это имел в виду", "я не это имела в виду", "нет спасибо"),
    "en": ("no", "nope", "nah", "no thanks", "not that", "no not that", "that's not it", "thats not it", "wrong",
           "that's wrong", "thats wrong", "incorrect", "cancel", "no cancel", "that's not what i meant",
           "thats not what i meant", "that is not what i meant", "not what i meant", "don't", "do not"),
    "de": ("nein", "nein danke", "nicht das", "nein nicht das", "falsch", "das ist falsch", "abbrechen", "abbruch",
           "das meinte ich nicht", "das habe ich nicht gemeint", "nicht nötig", "nicht notig"),
    "fr": ("non", "non merci", "pas ça", "pas ca", "non pas ça", "non pas ca", "faux", "c'est faux", "annule",
           "annuler", "ce n'est pas ça", "ce n'est pas ca", "ce n'est pas ce que je voulais dire"),
    "es": ("no", "no gracias", "eso no", "no eso no", "incorrecto", "cancela", "cancelar",
           "no es lo que quería decir", "no es lo que queria decir", "no me refería a eso", "no me referia a eso"),
    "it": ("no", "no grazie", "non quello", "no non quello", "sbagliato", "annulla", "non è quello che intendevo",
           "non e quello che intendevo", "non intendevo questo"),
    "pt": ("não", "nao", "não obrigado", "nao obrigado", "não obrigada", "nao obrigada", "isso não", "isso nao",
           "errado", "cancela", "cancelar", "não era isso", "nao era isso", "não é isso", "nao e isso",
           "não era isso que eu queria dizer", "nao era isso que eu queria dizer"),
}
_UNCLEAR = {
    "ru": ("не знаю", "не уверен", "не уверена", "наверное", "может быть", "может", "возможно", "хз",
           "даже не знаю", "сам не знаю", "сама не знаю", "посмотрим", "ну не знаю"),
    "en": ("maybe", "perhaps", "not sure", "i'm not sure", "im not sure", "i don't know", "i dont know",
           "dont know", "idk", "i guess", "possibly"),
    "de": ("vielleicht", "weiß nicht", "weiss nicht", "ich weiß nicht", "ich weiss nicht", "nicht sicher",
           "keine ahnung"),
    "fr": ("peut-être", "peut etre", "peut-etre", "je ne sais pas", "je sais pas", "pas sûr", "pas sur"),
    "es": ("quizás", "quizas", "tal vez", "no sé", "no se", "no estoy seguro", "no estoy segura"),
    "it": ("forse", "non so", "non lo so", "non sono sicuro", "non sono sicura"),
    "pt": ("talvez", "não sei", "nao sei", "não tenho certeza", "nao tenho certeza"),
}
_YES_TOKENS = frozenset({"да", "ага", "угу", "yes", "yeah", "yep", "ja", "oui", "sí", "si", "sì", "sim"})
_NEG_TOKENS = frozenset({"нет", "не", "неа", "no", "not", "nope", "nein", "nicht", "non", "pas", "não", "nao"})
_POLITE = frozenset({"пожалуйста", "please", "bitte", "merci", "gracias", "grazie", "obrigado", "obrigada", "спасибо",
                     "thanks", "danke"})
_POLITE_MULTI = ("s'il te plaît", "s'il vous plaît", "s'il te plait", "s'il vous plait", "por favor", "per favore")


def _norm(text: str) -> str:
    value = str(text or "").casefold().replace("ё", "е").replace("’", "'").replace("`", "'")
    for phrase in _POLITE_MULTI:
        value = value.replace(phrase, " ")
    value = re.sub(r"[.,!?…;:«»\"“”„()\[\]—–]+|(?<!\w)-|-(?!\w)", " ", value)
    words = value.split()
    out: list[str] = []
    for word in words:
        if out and out[-1] == word:
            continue  # «да да», «yes yes», «нет нет»
        out.append(word)
    while out and out[-1] in _POLITE and len(out) > 1:
        out.pop()
    while out and out[0] in _POLITE and len(out) > 1:
        out.pop(0)
    return " ".join(out)


@lru_cache(maxsize=16)
def _tables(code: str | None):
    codes = (code,) if code in _YES else tuple(_YES)
    pick = lambda table: frozenset(_norm(p) for c in codes for p in table.get(c, ()))
    return pick(_YES), pick(_NO), pick(_UNCLEAR)


def classify(text: str, code: str | None = None) -> str | None:
    """'yes' / 'no' / 'unclear' for a complete short reply, else None.

    ``code`` limits phrases to one language; None accepts the seven languages
    (for prompts whose own rules were already language-neutral).
    """
    if any(mark in str(text or "") for mark in '"«»“”„'):
        return None  # a quoted reply is literal data, never an answer
    value = _norm(text)
    if not value or len(value) > 80:
        return None
    yes, no, unclear = _tables(code)
    words = value.split()
    has_yes = any(w in _YES_TOKENS for w in words)
    has_neg = any(w in _NEG_TOKENS for w in words)
    if value in unclear:
        return UNCLEAR
    if value in no:
        return NO
    if value in yes:
        # «да нет» never appears in the yes table; a yes phrase is always clean.
        return YES
    if has_yes and has_neg and len(words) <= 4:
        return UNCLEAR  # «да нет», «да не», «нет да», «sì no»
    return None


def is_yes(text: str, code: str | None = None) -> bool:
    return classify(text, code) == YES


class _Guarded:
    """A legacy yes/no regex that never contradicts :func:`classify`."""

    def __init__(self, pattern, polarity: str) -> None:
        self._pattern, self._polarity = pattern, polarity
        self.pattern = getattr(pattern, "pattern", "")

    def _verdict(self, text):
        verdict = classify(text)
        if verdict is None:
            return None
        if verdict == self._polarity and self._polarity == NO:
            return None  # new negative wording keeps each prompt's own cancel rules
        return verdict == self._polarity

    def match(self, text, *args):
        verdict = self._verdict(text)
        if verdict is None:
            return self._pattern.match(text, *args)
        return _Hit(text) if verdict else None

    def fullmatch(self, text, *args):
        verdict = self._verdict(text)
        if verdict is None:
            return self._pattern.fullmatch(text, *args)
        return _Hit(text) if verdict else None

    def search(self, text, *args):
        verdict = self._verdict(text)
        if verdict is None:
            return self._pattern.search(text, *args)
        return _Hit(text) if verdict else None


class _Hit:
    def __init__(self, text) -> None:
        self._text = str(text)

    def group(self, index=0):
        return self._text

    def __bool__(self) -> bool:
        return True


def guard_yes(pattern):
    return _Guarded(pattern, YES)


def guard_no(pattern):
    return _Guarded(pattern, NO)
