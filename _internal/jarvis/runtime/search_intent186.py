"""SEARCH / CHAT / CLARIFY / NO-SEARCH decision for information questions.

USER-REQ-SEARCH-ROUTING-20261008-0209. The decision looks at the whole
question in the user's own language (not at a single word):

* an explicit «найди в интернете / загугли / search the web» means search;
* an explicit «ответь без поиска / without searching» forbids SearchAPI;
* «как ты думаешь, …» is only a lead-in: the inner question decides — a
  procedure/cause/fact question searches, a question about NEO or «us» chats;
* «как это сделать?» without a known subject asks what exactly is meant.

Runs only after every command/status/literal owner has declined the turn, so
«открой браузер» stays an action and «сколько осталось на таймере» stays local.
The function is pure: no I/O, no model, no secrets; the projected query is the
question itself without the search verb (never the dialogue).
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class SearchDecision:
    route: str = ""          # search | chat | clarify_topic | no_search | ""
    query: str = ""
    reason: str = ""


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("ё", "е").replace("’", "'")
    return re.sub(r"\s+", " ", value).strip()


_WEB_SHELL = re.compile(
    r"^(?:(?:пожалуйста|плиз|слушай|ну|а|нео|neo)[,\s]+)*(?:"
    r"за?г+у+г?л\w*|погугл\w*|гугл(?:ани|ни)?|найди\s+(?:мне\s+)?(?:в|во)\s+(?:интернете|сети|гугле|google)|"
    r"поищи\s+(?:в|во)\s+(?:интернете|сети|гугле)|посмотри\s+в\s+(?:интернете|сети|гугле)|"
    r"(?:теперь\s+)?найди\s+(?:инструкцию|информацию|ответ)\s+в\s+интернете|"
    r"google|search\s+(?:the\s+(?:web|internet)|online)\s+for|search\s+(?:the\s+(?:web|internet)|online)|look\s+(?:it\s+)?up\s+online|"
    r"find\s+(?:it\s+)?(?:on\s+the\s+(?:internet|web)|online)|"
    r"such(?:e)?\s+im\s+(?:internet|netz)(?:\s+nach)?|google\s+mal|"
    r"cherche\s+sur\s+(?:internet|le\s+web|google)|recherche\s+sur\s+internet|"
    r"busca\s+en\s+(?:internet|google|la\s+web)|búscalo\s+en\s+internet|"
    r"cerca\s+su\s+(?:internet|google)|cercalo\s+su\s+internet|"
    r"pesquisa\s+na\s+(?:internet|web)|procura\s+no\s+google|pesquise\s+na\s+internet"
    r")(?:\s*[:,]\s*|\s+|$)",
    re.I,
)
_NO_SEARCH = re.compile(
    r"^(?:(?:пожалуйста|ну|а)[,\s]+)*(?:ответь\s+без\s+(?:поиска|интернета|гугла)|без\s+(?:поиска|интернета)|не\s+(?:ищи|гугли)\s+в\s+интернете|"
    r"answer\s+without\s+(?:searching|search|the\s+internet|looking\s+it\s+up)|without\s+(?:searching|search)|don'?t\s+search|"
    r"antworte\s+ohne\s+(?:suche|internet)|ohne\s+(?:suche|internet)|"
    r"réponds\s+sans\s+(?:recherche|chercher|internet)|sans\s+(?:recherche|chercher)|"
    r"responde\s+sin\s+(?:buscar|búsqueda|internet)|sin\s+buscar|"
    r"rispondi\s+senza\s+(?:cercare|ricerca|internet)|senza\s+cercare|"
    r"responde\s+sem\s+(?:pesquisar|pesquisa|internet)|sem\s+pesquisar)(?:\s*[:,]\s*|\s+)",
    re.I,
)
_LEAD_IN = re.compile(
    r"^(?:(?:а|ну|слушай)[,\s]+)*(?:как\s+(?:ты\s+)?(?:думаешь|считаешь)|по-?твоему|по\s+твоему\s+мнению|что\s+скажешь|"
    r"in\s+your\s+opinion|what\s+do\s+you\s+think|do\s+you\s+think|you\s+think|"
    r"was\s+meinst\s+du|was\s+denkst\s+du|deiner\s+meinung\s+nach|meinst\s+du|"
    r"à\s+ton\s+avis|a\s+ton\s+avis|selon\s+toi|qu'en\s+penses-tu|d'après\s+toi|"
    r"¿?qué\s+opinas|¿?que\s+opinas|en\s+tu\s+opinión|¿?qué\s+piensas|¿?tú\s+qué\s+crees|"
    r"secondo\s+te|che\s+ne\s+pensi|cosa\s+ne\s+pensi|a\s+tuo\s+parere|"
    r"na\s+tua\s+opinião|na\s+sua\s+opinião|o\s+que\s+achas|o\s+que\s+você\s+acha|achas\s+que)\s*[,:]?\s*",
    re.I,
)
# A question about NEO itself or about «us»: conversation, not the web.
_SECOND_PERSON = re.compile(
    r"(?<![\w-])(?:ты|тебе|тебя|тобой|твой|твоя|твое|твои|твоих|мы|нас|нам|нами|наш|наша|наше|наши|нашего|нашей|"
    r"you|your|yours|we|us|our|ours|"
    r"du|dich|dir|dein|deine|deiner|wir|uns|unser|unsere|unserem|unserer|"
    r"tu|toi|te|ton|ta|tes|nous|notre|nos|"
    r"tú|ti|contigo|tuyo|tuya|nosotros|nuestra|nuestro|nuestras|nuestros|"
    r"tuo|tua|tuoi|noi|nostra|nostro|"
    r"teu|tua|você|nós|nossa|nosso|nossas|nossos)(?![\w-])",
    re.I,
)
_INFO_HEAD = re.compile(
    r"(?<![\w-])(?:как|каким\s+образом|почему|зачем|отчего|что\s+такое|кто\s+такой|кто\s+такая|что\s+значит|сколько|"
    r"how|why|what\s+is|what\s+are|what's|who\s+is|how\s+many|how\s+much|what\s+does|"
    r"wie|warum|wieso|weshalb|was\s+ist|was\s+sind|wer\s+ist|wie\s+viele|"
    r"comment|pourquoi|qu'est-ce\s+que|qu'est-ce\s+qu|qui\s+est|combien|"
    r"cómo|como|por\s+qué|porqué|qué\s+es|que\s+es|quién\s+es|cuántos|cuánto|"
    r"come|perché|perche|che\s+cos'è|cos'è|cosa\s+è|chi\s+è|quanti|quanto|"
    r"porque|por\s+que|o\s+que\s+é|quem\s+é|quantos|quanto)(?![\w-])",
    re.I,
)
_ADVICE = re.compile(
    r"^(?:(?:пожалуйста|ну|а)[,\s]+)*(?:подскажи(?:те)?|расскажи(?:те)?|объясни(?:те)?)\s+(?:мне\s+)?(?:способ|как|почему|что|зачем|рецепт|инструкци)"
    r"|^(?:tell|show|explain)\s+(?:me\s+)?(?:how|why|the\s+way)"
    r"|^(?:erkläre?|sag)\s+mir,?\s+(?:wie|warum)"
    r"|^(?:explique|dis)-moi\s+(?:comment|pourquoi)"
    r"|^(?:explícame|dime)\s+(?:cómo|por\s+qué)"
    r"|^(?:spiegami|dimmi)\s+(?:come|perché)"
    r"|^(?:explica-me|diz-me)\s+(?:como|porque)",
    re.I,
)
# Things the question could be about only through context («это», «it», «das»).
# Courtesy and request shells address NEO but do not make the question personal.
_COURTESY = re.compile(
    r"(?:,\s*)?(?:s['’]il\s+te\s+pla[iî]t|s['’]il\s+vous\s+pla[iî]t|stp|svp|por\s+favor|per\s+favore|per\s+piacere|se\s+faz\s+favor|"
    r"bitte|please|pls|пожалуйста|будь\s+добр\w*|если\s+(?:тебе\s+)?не\s+трудно)(?:\s*,)?",
    re.I,
)
_REQUEST = re.compile(
    r"^(?:(?:можешь|можете|не\s+подскажешь|не\s+подскажете|ты\s+не\s+подскажешь)\s+(?:мне\s+)?(?:подсказать|рассказать|объяснить|сказать)?\s*,?\s*|"
    r"(?:can|could|would)\s+you\s+(?:please\s+)?(?:tell|show|explain\s+to|explain)?\s*(?:me\s+)?|do\s+you\s+know\s+|"
    r"(?:kannst|könntest)\s+du\s+mir\s+(?:sagen|erklären|zeigen)\s*,?\s*|weißt\s+du\s*,?\s*|"
    r"(?:peux-tu|pourrais-tu|tu\s+peux)\s+(?:me\s+)?(?:dire|expliquer|montrer)\s*|sais-tu\s+|"
    r"(?:puedes|podrías|podrias)\s+(?:decirme|explicarme|mostrarme)\s*|¿?sabes\s+|"
    r"(?:puoi|potresti)\s+(?:dirmi|spiegarmi|mostrarmi)\s*|sai\s+|"
    r"(?:podes|pode|poderias)\s+(?:dizer-me|explicar-me|mostrar-me|me\s+dizer|me\s+explicar)\s*|sabes\s+)",
    re.I,
)
_DEICTIC = re.compile(
    r"^(?:а\s+)?(?:как|how|wie|comment|cómo|como|come)\s+(?:(?:мне|i|ich|je|yo|io|eu|do\s+i|can\s+i)\s+)?"
    r"(?:(?:это|его|ее|её|it|this|that|das|es|ça|cela|le|la|lo|eso|esto|questo|isso|isto)\s+)?"
    r"(?:сделать|делать|сделаю|do|make|mache|machen|faire|fais|hacer|hago|lo\s+hago|fare|faccio|si\s+fa|fazer|faço)"
    r"(?:\s+(?:это|его|it|this|that|das|es|ça|cela|eso|esto|questo|isso|isto))?\s*\??$",
    re.I,
)
# NEO 187 (REC-SEARCH-DIALOG-SOURCE-01): natural auxiliary/inverted forms of an
# open «how do I do that?» — a missing subject, not a search topic.
_DEICTIC_ALT = re.compile(
    r"^(?:а\s+)?(?:"
    r"как\s+(?:мне\s+)?(?:это|такое)\s+(?:сделать|делается|делать)|как\s+(?:я\s+)?(?:это|такое)\s+сделаю|"
    r"how\s+(?:do|can|should)\s+i\s+do\s+(?:it|this|that)|how\s+to\s+do\s+(?:it|this|that)|how\s+does\s+(?:it|this|that)\s+work|"
    r"wie\s+(?:mache|tue|schaffe)\s+ich\s+(?:das|es)|wie\s+geht\s+(?:das|es)|wie\s+macht\s+man\s+(?:das|es)|"
    r"comment\s+(?:est-ce\s+que\s+)?(?:je|on)\s+(?:le\s+|la\s+)?(?:fais|fait|faire)(?:\s+(?:ça|cela))?|comment\s+faire\s+(?:ça|cela)|"
    r"c[oó]mo\s+(?:lo\s+)?(?:hago|se\s+hace)(?:\s+(?:eso|esto))?|c[oó]mo\s+hacer\s+(?:eso|esto)|"
    r"come\s+(?:lo\s+)?(?:faccio|si\s+fa)(?:\s+(?:questo|ciò))?|come\s+fare\s+(?:questo|ciò)|"
    r"como\s+(?:[eé]\s+que\s+)?(?:eu\s+)?(?:o\s+)?fa[cç]o(?:\s+(?:isso|isto))?|como\s+se\s+faz(?:\s+(?:isso|isto))?|como\s+fazer\s+(?:isso|isto)"
    r")\s*\??$",
    re.I,
)
_IMPERATIVE_ACTION = re.compile(
    r"^(?:открой|закрой|запусти|включи|выключи|удали|создай|перемести|скопируй|напиши|отправь|поставь|сверни|разверни|"
    r"open|close|launch|start|run|play|stop|delete|create|move|copy|send|write|turn|"
    r"öffne|schließe|starte|spiele|lösche|erstelle|sende|ouvre|ferme|lance|joue|supprime|crée|envoie|"
    r"abre|cierra|inicia|reproduce|borra|crea|envía|apri|chiudi|avvia|riproduci|elimina|invia|"
    r"abre|fecha|inicia|reproduz|apaga|cria|envia)(?![\w-])",
    re.I,
)
_STOP = frozenset("""
как что кто где когда почему зачем сколько это мне меня я ли бы же а и в на с по к о об для из у
how what who where when why which do does did is are i me my a an the to of in on for it this that can should would
wie was wer wo wann warum ich mir mich ist sind ein eine der die das den zu von mit
comment que qui où quand pourquoi je me moi est un une le la les de du des à en pour
cómo como qué que quién dónde cuándo por yo me es un una el la los las de en para lo
come che chi dove quando perché io mi è un una il la lo gli di in per si
como que quem onde quando porque eu me é um uma o a os as de em para
сделать делать сделаю make do faire hacer fare fazer machen
""".split())

# «это», «инструкцию к этому», «more about it»: the subject comes from context.
_REFERENT = re.compile(
    r"(?:(?:инструкци\w*|информаци\w*|ответ|подробност\w*|instructions?|information|details?|more|anleitung|infos?|"
    r"instructions?|infos?|instrucciones|información|istruzioni|informazioni|instruções|informações)\s+)?"
    r"(?:(?:к|для|про|об?|по|on|for|about|to|zu|dazu|für|pour|sur|para|sobre|per|su)\s+)?"
    r"(?:это\w*|этом|этого|него|нему|it|this|that|das|dazu|ça|cela|eso|esto|questo|isso|isto)\s*\??",
    re.I,
)
_WANT = re.compile(
    r"^(?:я\s+)?(?:хочу|хотел\w*\s+бы|собираюсь|нужно|надо|мне\s+нужно|i\s+(?:want|would\s+like|need)\s+to|"
    r"ich\s+(?:möchte|will|muss)|je\s+(?:veux|voudrais|dois)|quiero|necesito|voglio|vorrei|devo|quero|preciso\s+de)\s+",
    re.I,
)
_CLARIFY = {
    "ru": "Что именно ты хочешь сделать?", "en": "What exactly do you want to do?",
    "de": "Was genau möchtest du tun?", "fr": "Que veux-tu faire exactement ?",
    "es": "¿Qué quieres hacer exactamente?", "it": "Cosa vuoi fare esattamente?",
    "pt": "O que queres fazer exatamente?",
}
_NO_SEARCH_NOTE = {
    "ru": "Отвечаю без поиска в интернете — это не проверено по источникам.",
    "en": "Answering without a web search — this is not checked against sources.",
    "de": "Antwort ohne Websuche — nicht anhand von Quellen geprüft.",
    "fr": "Réponse sans recherche sur Internet — non vérifiée par des sources.",
    "es": "Respondo sin buscar en internet; no está contrastado con fuentes.",
    "it": "Rispondo senza cercare in rete: non è verificato con fonti.",
    "pt": "Respondo sem pesquisar na internet; não foi verificado em fontes.",
}


def clarify_text(code: str) -> str:
    return _CLARIFY.get(str(code or "en")[:2], _CLARIFY["en"])


def no_search_note(code: str) -> str:
    return _NO_SEARCH_NOTE.get(str(code or "en")[:2], _NO_SEARCH_NOTE["en"])


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]+", _fold(text)) if w not in _STOP and len(w) >= 3]


def _strip_punct(text: str) -> str:
    return str(text or "").strip().strip("¿¡").strip(" «»\"'").strip()


_FIND_REFERENT = re.compile(
    r"^(?:(?:теперь|а|ну)\s+)?(?:найди|поищи|покажи|find|search\s+for|look\s+up|get|such\w*|finde|cherche|trouve|busca|encuentra|cerca|trova|procura|pesquisa)\s+"
    r"(?:мне\s+|me\s+|mir\s+|moi\s+|me\s+)?(?:(?:the|an?|die|eine|des|la|une|las|unas|le|delle|as|umas)\s+)?"
    r"(?:инструкци\w*|информаци\w*|ответ\w*|подробност\w*|instructions?|information|details|anleitung\w*|infos?|"
    r"informations?|instrucciones|información|istruzioni|informazioni|instruções|informações)\s+"
    r"(?:к|для|про|по|о|об|on|for|about|zu|dazu|für|pour|sur|para|sobre|per|su)\s+"
    r"(?:этому|этого|этом|нему|него|it|this|that|ça|cela|eso|esto|questo|isso|isto)\s*\??$",
    re.I,
)


def _clean_query(text: str) -> str:
    value = re.sub(r"[,\s]+(?:подскажи(?:те)?|скажи(?:те)?|please|bitte)\s*(?=[?.!]|$)", "", str(text or ""), flags=re.I)
    value = re.sub(r"\s+([?!.,])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip(" ,")


def decide(text: str, code: str = "ru", *, topic: str = "") -> SearchDecision:
    decision = _decide(text, code, topic=topic)
    if decision.query:
        return SearchDecision(decision.route, _clean_query(decision.query), decision.reason)
    return decision


def _decide(text: str, code: str = "ru", *, topic: str = "") -> SearchDecision:
    original = _strip_punct(text)
    value = _fold(original)
    if not value or len(value) > 400:
        return SearchDecision()
    if _FIND_REFERENT.match(value):
        if topic:
            return SearchDecision("search", topic, "explicit_web_with_context")
        return SearchDecision("clarify_topic", "", "referent_unknown")
    no_search = _NO_SEARCH.match(value)
    if no_search:
        inner = _strip_punct(original[no_search.end():]) or original
        return SearchDecision("no_search", inner, "explicit_no_search")
    web = _WEB_SHELL.match(value)
    if web:
        inner = _strip_punct(original[web.end():])
        if not inner:
            if topic:
                return SearchDecision("search", topic, "explicit_web_with_context")
            return SearchDecision("clarify_topic", "", "web_without_subject")
        if _DEICTIC.match(_fold(inner)) or _DEICTIC_ALT.match(_fold(inner)) or _REFERENT.fullmatch(_fold(inner)):
            if topic:
                return SearchDecision("search", topic, "explicit_web_with_context")
            return SearchDecision("clarify_topic", "", "explicit_web_deictic")
        return SearchDecision("search", inner, "explicit_web")
    if _IMPERATIVE_ACTION.match(value) and not _INFO_HEAD.search(value):
        return SearchDecision()           # an action request stays with its owner
    from jarvis.runtime import web_answers171
    if web_answers171.SMALL_TALK.match(value):
        return SearchDecision("chat", "", "small_talk")
    if web_answers171._LOCAL.search(value):
        return SearchDecision()           # this computer's files/system: local owners
    value = _COURTESY.sub(" ", value).strip(" ,")
    original = _COURTESY.sub(" ", original).strip(" ,")
    value = re.sub(r"\s+", " ", value)
    original = re.sub(r"\s+", " ", original)
    request = _REQUEST.match(value)
    if request and request.end() < len(value):
        value = value[request.end():].strip()
        original = original[len(original) - len(value):] if len(original) >= len(value) else original
    lead = _LEAD_IN.match(value)
    inner_value = value[lead.end():] if lead else value
    inner_original = _strip_punct(original[len(original) - len(inner_value):]) if lead else original
    if lead and not inner_value.strip(" ?"):
        return SearchDecision("chat", "", "opinion_only")
    if _SECOND_PERSON.search(inner_value) or web_answers171._CHAT.search(inner_value):
        return SearchDecision("chat", "", "about_neo_or_us")
    if _DEICTIC.match(inner_value) or _DEICTIC_ALT.match(inner_value):
        if topic:
            return SearchDecision("search", inner_original.rstrip("?") + " " + topic, "deictic_with_context")
        return SearchDecision("clarify_topic", "", "unknown_subject")
    informative = bool(_INFO_HEAD.search(inner_value) or _ADVICE.match(inner_value))
    if informative and _content_words(inner_value):
        return SearchDecision("search", inner_original, "information_question")
    return SearchDecision()


_NON_ANSWER187 = re.compile(
    r"^(?:да|нет|не\s+знаю|я\s+не\s+знаю|без\s+понятия|ага|угу|хз|возможно|может\s+быть|ок|окей|хорошо|не\s+уверен[а]?|"
    r"yes|yeah|yep|no|nope|i\s+don'?t\s+know|don'?t\s+know|idk|no\s+idea|maybe|ok|okay|sure|not\s+sure|"
    r"ja|nein|wei(?:ß|ss)\s+nicht|ich\s+wei(?:ß|ss)\s+(?:es\s+)?nicht|keine\s+ahnung|vielleicht|okay|ok|"
    r"oui|non|je\s+(?:ne\s+)?sais\s+pas|aucune\s+idée|peut-être|d'accord|"
    r"sí|si|no|no\s+sé|no\s+lo\s+sé|ni\s+idea|quizás|quizá|tal\s+vez|vale|"
    r"sì|no|non\s+lo\s+so|non\s+so|forse|va\s+bene|boh|"
    r"sim|não|nao|não\s+sei|nao\s+sei|talvez|está\s+bem|ok)\s*[.!?…]*$",
    re.I,
)


def is_non_answer(text: str) -> bool:
    """«Да», «не знаю», «maybe» — consent or uncertainty, never a search subject."""
    return bool(_NON_ANSWER187.match(" ".join(str(text or "").replace("’", "'").casefold().split())))


_CANCEL = re.compile(r"^(?:отмена|отмени|не\s+надо|забудь|стоп|cancel|never\s*mind|forget\s+it|abbrechen|vergiss\s+es|annule|laisse\s+tomber|"
                     r"cancela|olvídalo|annulla|lascia\s+perdere|cancelar|esquece)(?![\w-])", re.I)


def is_cancel(text: str) -> bool:
    return bool(_CANCEL.match(_fold(text)))


_INFINITIVE = re.compile(r"^(?:\w+(?:ть|ться|чь)|to\s+\w+|\w+(?:en|n)|\w+(?:er|ir|re)|\w+(?:ar|er|ir)|\w+(?:are|ere|ire)|\w+(?:ar|er|ir))(?:\s|$)", re.I)


def topic_core(text: str) -> str:
    """The subject of a statement: «Я хочу сложить бумажный самолётик» → «сложить бумажный самолётик»."""
    value = _strip_punct(text).rstrip("?.!").strip()
    value = re.sub(r"^(?:а|ну|так|ok|okay|well|also|alors|bueno|allora|então)[,\s]+", "", value, flags=re.I)
    return _WANT.sub("", value).strip()


def merge_topic(question: str, topic: str) -> str:
    """«Как это сделать?» + «Бумажный самолётик» → «Как сделать бумажный самолётик»;
    + «сложить бумажный самолётик» → «Как сложить бумажный самолётик»."""
    q = _strip_punct(question).rstrip("?").strip()
    q = re.sub(r"^(?:а|and|und|et|y|e)\s+", "", q, flags=re.I)
    t = topic_core(topic)
    if not t:
        return q
    head = re.match(r"(?i)^(как|how(?:\s+do\s+i|\s+can\s+i)?|wie|comment|cómo|como|come)\b", q)
    first = t.split()[0].casefold()
    if head and (first.endswith(("ть", "ться", "чь")) or first in {"to"}):
        return f"{head.group(1)} {t}"
    replaced = re.sub(r"(?<![\w-])(?:это|его|it|this|that|das|ça|cela|eso|esto|questo|isso|isto)(?![\w-])\s*", "", q, count=1, flags=re.I)
    return re.sub(r"\s+", " ", f"{replaced} {t}").strip()


def topic_candidate(text: str) -> str:
    """A non-command statement that can give «это» its subject in the next turn."""
    value = _strip_punct(text)
    folded = _fold(value)
    if not folded or len(folded) > 200 or _IMPERATIVE_ACTION.match(folded):
        return ""
    core = topic_core(value)
    return core if _content_words(core) else ""
