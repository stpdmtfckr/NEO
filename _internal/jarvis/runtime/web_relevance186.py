"""Query/answer relevance for retrieved web snippets (SQ-01…SQ-04, USER185-09).

Deterministic and cheap: no model, no network. A candidate answer is scored by
how well it covers the topic of the question, whether it *defines* the asked
entity (general question) rather than a narrower variety of it, and whether it
looks like a forum/advertising fragment. The first provider row is evidence, not
an automatic winner.
"""
from __future__ import annotations

import re
import unicodedata

_SEARCH_VERBS = re.compile(
    r"^(?:(?:пожалуйста|плиз|слушай|ну)\s+)*(?:загугли(?:ть)?|заг+угли|погугли|гугл(?:ани)?|найди(?:\s+в\s+(?:интернете|гугле|сети))?|"
    r"поищи(?:\s+в\s+(?:интернете|гугле|сети))?|посмотри\s+в\s+интернете|узнай|"
    r"google|search(?:\s+for)?|look\s+up|find(?:\s+out)?|"
    r"googel?n?|suche(?:\s+nach)?|recherche|cherche|busca|buscar|cerca|pesquisa|procura)\s*[,:]?\s+",
    re.I,
)
_QUESTION_WORDS = frozenset("""
что такое кто такой такая такие это как какой какая какое какие где когда почему зачем сколько чем
what is are who was were how why where when which does do the a an of about
was ist sind wer wie warum wo wann welche der die das ein eine
qu est ce que qui comment pourquoi où quand quel quelle les le la un une des du de
qué que es son quién cómo por dónde cuándo cuál el los las uno una del
cos è che chi come perché dove quando quale il lo gli uno
o que é quem como porque onde quando qual os as um uma do da
""".split())
_STOP = _QUESTION_WORDS | frozenset("""
и в во на с со по к ко от до за для из у о об а но или же ли бы не
and or for in on at to from with by is it this that be
und oder für in auf mit von zu im ist es
et ou pour dans sur avec par est il elle ce
y o para en con por es
e o per in su con da è
e ou para em com por é
""".split())
_DEFINE = re.compile(
    r"^\s*(?:это\s+|—\s*это\s+)?(?:—|-|–|:|\s+это\b|\s+—\s+это\b|\s+is\s+(?:a|an|the)\b|\s+are\b|\s+(?:ist|sind)\s+(?:ein|eine|die|der)\b|"
    r"\s+(?:est|sont)\s+(?:un|une|le|la)\b|\s+(?:es|son)\s+(?:un|una|el|la)\b|\s+(?:è|sono)\s+(?:un|una|il|la)\b|\s+(?:é|são)\s+(?:um|uma|o|a)\b|"
    r"\s*\()",
    re.I,
)
_NOISE = re.compile(
    r"(?:купить|купите|скидк\w*|акци\w*|доставк\w*|в\s+наличии|цена\s+от|руб\.\s*$|заказать|интернет-магазин|"
    r"форум|отвечу|подскажите|помогите|у\s+меня\s+(?:есть|была)|по-моему|имхо|"
    r"\bbuy\b|\bsale\b|discount|order\s+now|free\s+shipping|forum|in\s+my\s+opinion|\bimho\b|"
    r"kaufen|angebot|acheter|promo|comprar|oferta|acquista|offerta)",
    re.I,
)
_PRICE_TOPIC = re.compile(r"(?:цен|стоим|стоит|купить|price|cost|buy|preis|prix|precio|prezzo|pre[cç]o)", re.I)


def fold(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("ё", "е").replace("’", "'")
    return re.sub(r"\s+", " ", value).strip()


def canonical_question(text: str) -> str:
    """The question without the search verb, ё/е, case and closing punctuation."""
    value = fold(text)
    for _ in range(2):
        value = _SEARCH_VERBS.sub("", value)
    return re.sub(r"[\s.!?…,;:]+$", "", value).strip(" «»\"'")


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:[+#]+)?", fold(text))


def topic_terms(question: str) -> list[str]:
    return [w for w in _words(canonical_question(question)) if w not in _STOP and len(w) >= 2]


def _stem(word: str) -> str:
    return word[:5] if len(word) > 5 else word


def is_echo(answer: str, question: str) -> bool:
    """An answer that only repeats the question carries no information."""
    a = canonical_question(answer)
    q = canonical_question(question)
    if not a:
        return True
    a_words = [w for w in _words(a) if w not in _STOP] or _words(a)
    q_words = [w for w in _words(q) if w not in _STOP] or _words(q)
    if not a_words:
        return True
    return set(map(_stem, a_words)) <= set(map(_stem, q_words)) and len(a) <= len(q) + 12


def is_noise(answer: str, question: str = "") -> bool:
    """Advertising or forum chatter (unless the question itself is about prices)."""
    return bool(_NOISE.search(fold(answer))) and not _PRICE_TOPIC.search(fold(question))


def score(answer: str, question: str) -> float:
    """Higher is better; <= 0 means 'not an answer to this question'."""
    text = fold(answer)
    if not text or is_echo(answer, question):
        return -1.0
    terms = topic_terms(question)
    if not terms:
        return 0.5
    stems = {_stem(t) for t in terms}
    words = _words(text)
    present = {_stem(w) for w in words} & stems
    value = 2.0 * len(present) / len(stems)
    head = text[:80]
    first_term = next((i for i, w in enumerate(words) if _stem(w) in stems), -1)
    if first_term == 0:
        rest = text[len(words[0]):] if words else ""
        if _DEFINE.match(rest) or _DEFINE.match(" " + rest):
            value += 1.2      # «Берёза — род…»: the entity itself is defined
        else:
            value += 0.5
    elif 0 < first_term <= 2:
        modifier = words[first_term - 1]
        if modifier not in _STOP and _stem(modifier) not in stems and len(stems) == 1:
            value -= 0.6      # «Карельская берёза …»: a narrower variety
        else:
            value += 0.2
    elif first_term < 0:
        value -= 0.5
    if any(_stem(w) in stems for w in _words(head)):
        value += 0.2
    if _NOISE.search(text) and not _PRICE_TOPIC.search(fold(question)):
        value -= 1.2
    if text.endswith("?") or text.count("!") >= 2:
        value -= 0.6
    return value


def related_is_useful(candidate: str, question: str, answer: str = "") -> bool:
    """SQ-04: a follow-up must stay on topic and add something new."""
    c = canonical_question(candidate)
    q = canonical_question(question)
    if not c or c == q:
        return False
    c_stems = {_stem(w) for w in _words(c) if w not in _STOP}
    q_stems = {_stem(w) for w in topic_terms(question)}
    if not c_stems or not q_stems or not (c_stems & q_stems):
        return False
    if c_stems <= q_stems:
        return False   # the same question in other words
    return True
