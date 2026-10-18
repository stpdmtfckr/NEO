# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Context-aware personal addressing for NEO JARVIS.

HOTFIX112 replaces the old one-size-fits-all "acknowledgement prefix" with a
small deterministic social/command policy.  The user's configured name remains
data only; this module merely decides *how* it is natural to address that name
for the current utterance.

The policy deliberately stays independent from the LLM.  Phrase Atlas commands
can therefore keep their sub-millisecond deterministic path, while greetings,
thanks, farewells and ordinary questions no longer receive odd phrases such as
"Принял, Босс" / "Understood, Alex".
"""

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from typing import Any


SUPPORTED = frozenset({"ru", "en", "de", "fr", "es", "it", "pt"})

# Atlas actions whose result is fundamentally informational/read-only.  Even when
# the user used an imperative shell ("покажи статус"), a neutral name address is
# more natural than military acknowledgement.
_INFORMATION_ACTIONS = frozenset({
    "active", "all", "api_status", "background_status", "count", "cpu",
    "current", "date_info", "disks", "forecast", "get_power_plan", "gpu",
    "humidity", "info", "latency", "latest", "list", "list_devices",
    "list_drives", "list_power_plans", "memory", "music_queue_status",
    "network", "objects", "plugins", "precipitation", "queue_status", "rain",
    "recent", "self_test", "session_info", "show_session", "state", "status",
    "summary", "sunrise", "sunset", "temperature", "today", "tomorrow",
    "top_processes", "upcoming", "wind",
})

# Canonical internal command verbs produced by multilingual command normalization.
_ACTION_VERBS = frozenset({
    "открой", "закрой", "запусти", "покажи", "скрой", "найди", "создай",
    "скопируй", "перемести", "переименуй", "удали", "прочитай", "напиши",
    "вставь", "очисти", "сохрани", "отправь", "скачай", "установи",
    "подключись", "отключись", "сверни", "разверни", "восстанови",
    "переключи", "перейди", "нажми", "выбери", "включи", "выключи",
    "пауза", "стоп", "продолжи", "дальше", "скип", "назад", "перемотай",
    "перемешай", "повтор", "лайк", "дизлайк", "громче", "тише", "приглуши",
    "заблокируй", "перезагрузи", "усыпи", "запомни", "забудь", "переведи",
    "сделай", "выполни",
})

# These command-looking words are conversational requests rather than a computer
# action.  Prefixing them with "Принял" is usually stilted; use a neutral name.
_CONVERSATIONAL_VERBS = frozenset({"расскажи", "объясни", "помоги", "проанализируй"})


@dataclass(frozen=True)
class AddressContext:
    kind: str = "neutral"          # action | neutral | greeting | gratitude | farewell | reassurance | self_addressed
    greeting: str = "general"      # general | morning | day | evening
    atlas_action: str = ""
    atlas_intent: str = ""


def _code(code: str) -> str:
    value = str(code or "ru").strip().casefold()
    return value if value in SUPPORTED else "ru"


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("’", "'")
    value = re.sub(r"[^0-9a-zà-öø-ÿа-яё]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def _starts_with_phrase(value: str, phrases: tuple[str, ...]) -> str:
    padded = value + " "
    for phrase in sorted(phrases, key=len, reverse=True):
        p = _norm(phrase)
        if value == p or padded.startswith(p + " "):
            return p
    return ""


@lru_cache(maxsize=7)
def _locale_action_aliases(code: str) -> tuple[tuple[str, str], ...]:
    """Return selected-locale command shells mapped to canonical internal verbs.

    This reads only the currently requested locale file and mirrors the strict
    language isolation policy used by multilingual command normalization. It is
    used for *address style only* and never routes/executes commands.
    """
    code = _code(code)
    path = Path(__file__).resolve().parents[1] / "locales" / "commands" / f"{code}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        aliases = payload.get("aliases", {}) if isinstance(payload, dict) else {}
    except Exception:
        aliases = {}
    rows: list[tuple[str, str]] = []
    for canonical in sorted(_ACTION_VERBS | _CONVERSATIONAL_VERBS):
        values = aliases.get(canonical, []) if isinstance(aliases, dict) else []
        if not isinstance(values, list):
            continue
        for alias in values:
            value = _norm(alias)
            if value:
                rows.append((value, canonical))
    rows.sort(key=lambda row: len(row[0]), reverse=True)
    return tuple(rows)


def _locale_action_first(text: str, code: str) -> str:
    value = _norm(text)
    padded = value + " "
    for alias, canonical in _locale_action_aliases(code):
        if value == alias or padded.startswith(alias + " "):
            return canonical
    return ""


_SOCIAL: dict[str, dict[str, tuple[str, ...]]] = {
    "ru": {
        "greeting_general": ("привет", "здравствуй", "здравствуйте", "салют"),
        "greeting_morning": ("доброе утро",),
        "greeting_day": ("добрый день",),
        "greeting_evening": ("добрый вечер",),
        "gratitude": ("спасибо", "благодарю", "спс", "большое спасибо"),
        "farewell": ("пока", "до свидания", "до встречи", "увидимся", "спокойной ночи"),
        "apology": ("извини", "извините", "прости", "простите"),
        "how_are_you": ("как дела", "как ты", "как твои дела", "что нового"),
        "presence": ("ты тут", "ты здесь", "ты на связи", "тут", "здесь", "на связи"),
    },
    "en": {
        "greeting_general": ("hello", "hi", "hey"),
        "greeting_morning": ("good morning",),
        "greeting_day": ("good afternoon",),
        "greeting_evening": ("good evening",),
        "gratitude": ("thank you", "thanks", "many thanks", "much appreciated"),
        "farewell": ("bye", "goodbye", "see you", "see you later", "good night"),
        "apology": ("sorry", "excuse me"),
        "how_are_you": ("how are you", "how's it going", "how is it going", "what's new", "what is new"),
        "presence": ("are you there", "you there", "are you here"),
    },
    "de": {
        "greeting_general": ("hallo", "hi", "hey"),
        "greeting_morning": ("guten morgen",),
        "greeting_day": ("guten tag",),
        "greeting_evening": ("guten abend",),
        "gratitude": ("danke", "vielen dank", "besten dank"),
        "farewell": ("tschüss", "tschuss", "auf wiedersehen", "bis bald", "bis später", "gute nacht"),
        "apology": ("entschuldigung", "tut mir leid", "verzeihung"),
        "how_are_you": ("wie geht es dir", "wie geht's", "wie gehts", "was gibt's neues", "was gibt es neues"),
        "presence": ("bist du da", "bist du hier"),
    },
    "fr": {
        "greeting_general": ("bonjour", "salut", "coucou"),
        "greeting_morning": ("bonjour",),
        "greeting_day": ("bonjour",),
        "greeting_evening": ("bonsoir",),
        "gratitude": ("merci", "merci beaucoup"),
        "farewell": ("au revoir", "à bientôt", "a bientôt", "à plus", "a plus", "bonne nuit"),
        "apology": ("désolé", "desole", "désolée", "desolee", "pardon", "excuse-moi", "excusez-moi"),
        "how_are_you": ("comment ça va", "comment ca va", "ça va", "ca va", "quoi de neuf"),
        "presence": ("tu es là", "tu es la", "vous êtes là", "vous etes la"),
    },
    "es": {
        "greeting_general": ("hola", "buenas"),
        "greeting_morning": ("buenos días", "buenos dias"),
        "greeting_day": ("buenas tardes",),
        "greeting_evening": ("buenas noches",),
        "gratitude": ("gracias", "muchas gracias"),
        "farewell": ("adiós", "adios", "hasta luego", "hasta pronto", "nos vemos", "buenas noches"),
        "apology": ("perdón", "perdon", "lo siento", "disculpa", "disculpe"),
        "how_are_you": ("cómo estás", "como estas", "qué tal", "que tal", "qué hay de nuevo", "que hay de nuevo"),
        "presence": ("estás ahí", "estas ahi", "estás aquí", "estas aqui"),
    },
    "it": {
        "greeting_general": ("ciao", "salve"),
        "greeting_morning": ("buongiorno",),
        "greeting_day": ("buongiorno",),
        "greeting_evening": ("buonasera",),
        "gratitude": ("grazie", "grazie mille"),
        "farewell": ("arrivederci", "a presto", "ci vediamo", "buonanotte"),
        "apology": ("scusa", "scusi", "mi dispiace"),
        "how_are_you": ("come stai", "come va", "che c'è di nuovo", "che c e di nuovo"),
        "presence": ("sei lì", "sei li", "sei qui"),
    },
    "pt": {
        "greeting_general": ("olá", "ola", "oi"),
        "greeting_morning": ("bom dia",),
        "greeting_day": ("boa tarde",),
        "greeting_evening": ("boa noite",),
        "gratitude": ("obrigado", "obrigada", "muito obrigado", "muito obrigada", "valeu"),
        "farewell": ("tchau", "adeus", "até logo", "ate logo", "até mais", "ate mais", "boa noite"),
        "apology": ("desculpa", "desculpe", "sinto muito"),
        "how_are_you": ("como você está", "como voce esta", "como estás", "como estas", "tudo bem", "o que há de novo", "o que ha de novo"),
        "presence": ("você está aí", "voce esta ai", "está aí", "esta ai", "você está aqui", "voce esta aqui"),
    },
}

_QUESTION_WORDS: dict[str, tuple[str, ...]] = {
    "ru": ("кто", "что", "где", "когда", "зачем", "почему", "как", "какой", "какая", "какие", "сколько", "можно ли", "есть ли"),
    "en": ("who", "what", "where", "when", "why", "how", "which", "can", "could", "would", "is", "are", "do", "does", "did"),
    "de": ("wer", "was", "wo", "wann", "warum", "wieso", "wie", "welche", "welcher", "kann", "ist", "sind"),
    "fr": ("qui", "quoi", "où", "ou", "quand", "pourquoi", "comment", "quel", "quelle", "est-ce", "peux-tu", "pouvez-vous"),
    "es": ("quién", "quien", "qué", "que", "dónde", "donde", "cuándo", "cuando", "por qué", "por que", "cómo", "como", "cuál", "cual", "puedes", "es", "hay"),
    "it": ("chi", "cosa", "dove", "quando", "perché", "perche", "come", "quale", "puoi", "è", "e", "sono"),
    "pt": ("quem", "o que", "onde", "quando", "por que", "porque", "como", "qual", "pode", "é", "e", "há", "ha"),
}


def social_kind(text: str, code: str) -> tuple[str, str]:
    """Return ``(kind, subtype)`` for strong social shells, else ``("", "")``.

    Greeting is allowed as a prefix of a larger utterance ("привет, открой ...")
    because the greeting should still own the salutation. Thanks/farewell/apology
    are intentionally stricter to avoid misclassifying payloads such as song names.
    """
    code = _code(code)
    value = _norm(text)
    if not value:
        return "", ""
    p = _SOCIAL[code]
    for bucket, subtype in (
        ("greeting_morning", "morning"),
        ("greeting_day", "day"),
        ("greeting_evening", "evening"),
        ("greeting_general", "general"),
    ):
        if _starts_with_phrase(value, p[bucket]):
            return "greeting", subtype

    # Permit assistant-name/filler and a "how are you" tail on otherwise pure
    # social utterances.  This keeps voice phrases natural without matching
    # arbitrary commands/payloads.
    assistant_fillers = {
        "ru": ("нео", "нио", "neo", "джарвис", "джервис", "jarvis", "пожалуйста"),
        "en": ("neo", "jarvis", "please"),
        "de": ("neo", "jarvis", "bitte"),
        "fr": ("neo", "néo", "jarvis", "s'il te plaît", "s il te plait", "s'il vous plaît", "s il vous plait"),
        "es": ("neo", "jarvis", "por favor"),
        "it": ("neo", "jarvis", "per favore"),
        "pt": ("neo", "jarvis", "por favor"),
    }[code]
    stripped = value
    for filler in assistant_fillers:
        stripped = re.sub(rf"(?:^|\s){re.escape(_norm(filler))}(?=\s|$)", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()

    for bucket, kind in (("gratitude", "gratitude"), ("farewell", "farewell"), ("apology", "reassurance")):
        for phrase in p[bucket]:
            if stripped == _norm(phrase):
                return kind, "general"
    return "", ""


def classify_address_context(
    text: str,
    code: str,
    *,
    atlas_action: str = "",
    atlas_intent: str = "",
    canonical_text: str = "",
) -> AddressContext:
    """Choose a natural addressing style for one user turn.

    Exact Phrase Atlas metadata is preferred when available.  The linguistic
    fallback only decides between action acknowledgement and neutral addressing;
    it never routes or executes a command itself.
    """
    code = _code(code)
    kind, subtype = social_kind(text, code)
    if kind:
        return AddressContext(kind=kind, greeting=subtype, atlas_action=atlas_action, atlas_intent=atlas_intent)

    value = _norm(text)
    p = _SOCIAL[code]
    # Pure "how are you" and presence checks are conversation, never actions.
    if any(value == _norm(x) for x in p["how_are_you"] + p["presence"]):
        return AddressContext(kind="neutral", atlas_action=atlas_action, atlas_intent=atlas_intent)

    # Daily briefing owns its greeting/name itself because it must begin with a
    # time-of-day salutation ("Доброе утро, Босс"), not the generic outer
    # informational prefix. This also prevents duplicated names on manual calls.
    if str(atlas_intent or "").casefold() == "briefing.summary":
        return AddressContext(kind="self_addressed", atlas_action=atlas_action, atlas_intent=atlas_intent)

    action = str(atlas_action or "").strip().casefold()
    if action:
        return AddressContext(
            kind="neutral" if action in _INFORMATION_ACTIONS else "action",
            atlas_action=action,
            atlas_intent=str(atlas_intent or ""),
        )

    # Explicit questions are neutral even when they mention a command domain.
    if "?" in str(text or ""):
        return AddressContext(kind="neutral")
    for q in _QUESTION_WORDS[code]:
        qn = _norm(q)
        if value == qn or value.startswith(qn + " "):
            return AddressContext(kind="neutral")

    canonical = _norm(canonical_text or text)
    first = canonical.split(" ", 1)[0] if canonical else ""
    if first in _CONVERSATIONAL_VERBS:
        return AddressContext(kind="neutral")
    if first in _ACTION_VERBS:
        return AddressContext(kind="action")
    # The command normalizer deliberately refuses ambiguous fuzzy rewrites. For
    # address style we can still safely inspect an *exact* selected-locale shell
    # at the beginning of the utterance (e.g. Portuguese «abra navegador»).
    locale_first = _locale_action_first(text, code)
    if locale_first in _CONVERSATIONAL_VERBS:
        return AddressContext(kind="neutral")
    if locale_first in _ACTION_VERBS:
        return AddressContext(kind="action")
    return AddressContext(kind="neutral")


_PREFIXES: dict[str, dict[str, Any]] = {
    "ru": {
        "action": ("Так точно", "Принял", "Хорошо", "Сделаю", "Понял", "Уже делаю", "Конечно"),
        "neutral": ("{name}. ", "Смотри, {name}. ", "Итак, {name}. ", "Вот что есть, {name}. "),
        "gratitude": ("Пожалуйста", "Всегда пожалуйста", "Рад помочь", "Обращайся"),
        "farewell": ("До встречи", "Буду на связи", "Пока", "Береги себя"),
        "reassurance": ("Всё в порядке", "Ничего страшного", "Не переживай", "Разберёмся"),
        "greeting": {"general":"Привет", "morning":"Доброе утро", "day":"Добрый день", "evening":"Добрый вечер"},
    },
    "en": {
        "action": ("Certainly", "Understood", "All right", "On it", "Got it", "Sure thing", "Of course"),
        "neutral": ("{name}. ", "Here you go, {name}. ", "So, {name}. ", "Here's what I have, {name}. "),
        "gratitude": ("You're welcome", "Anytime", "Happy to help", "Glad I could help"),
        "farewell": ("See you", "I'll be here", "Bye", "Have a good one"),
        "reassurance": ("No problem", "It's all right", "Don't worry", "We'll sort it out"),
        "greeting": {"general":"Hello", "morning":"Good morning", "day":"Good afternoon", "evening":"Good evening"},
    },
    "de": {
        "action": ("Jawohl", "Verstanden", "In Ordnung", "Wird gemacht", "Alles klar", "Sofort", "Natürlich"),
        "neutral": ("{name}. ", "Schau, {name}. ", "Also, {name}. ", "Hier ist es, {name}. "),
        "gratitude": ("Gern geschehen", "Jederzeit gern", "Freut mich, dass ich helfen konnte", "Immer gern"),
        "farewell": ("Bis bald", "Ich bin da, wenn du mich brauchst", "Tschüss", "Mach’s gut"),
        "reassurance": ("Kein Problem", "Alles in Ordnung", "Keine Sorge", "Das kriegen wir hin"),
        "greeting": {"general":"Hallo", "morning":"Guten Morgen", "day":"Guten Tag", "evening":"Guten Abend"},
    },
    "fr": {
        "action": ("Bien reçu", "Compris", "D'accord", "Je m'en occupe", "C'est noté", "Tout de suite", "Bien sûr"),
        "neutral": ("{name}. ", "Regarde, {name}. ", "Alors, {name}. ", "Voilà, {name}. "),
        "gratitude": ("Avec plaisir", "Je t'en prie", "Ravi d'avoir aidé", "À ton service"),
        "farewell": ("À bientôt", "Je reste disponible", "Salut", "Prends soin de toi"),
        "reassurance": ("Pas de problème", "Tout va bien", "Ne t'inquiète pas", "On va arranger ça"),
        "greeting": {"general":"Bonjour", "morning":"Bonjour", "day":"Bonjour", "evening":"Bonsoir"},
    },
    "es": {
        "action": ("Entendido", "De acuerdo", "Claro", "Ahora mismo", "Vale", "Me encargo", "Por supuesto"),
        "neutral": ("{name}. ", "Mira, {name}. ", "Bueno, {name}. ", "Aquí tienes, {name}. "),
        "gratitude": ("De nada", "Con gusto", "Encantado de ayudar", "Para eso estoy"),
        "farewell": ("Hasta luego", "Aquí estaré", "Chao", "Cuídate"),
        "reassurance": ("No pasa nada", "Todo bien", "No te preocupes", "Lo arreglamos"),
        "greeting": {"general":"Hola", "morning":"Buenos días", "day":"Buenas tardes", "evening":"Buenas noches"},
    },
    "it": {
        "action": ("Ricevuto", "Capito", "Va bene", "Subito", "D'accordo", "Ci penso io", "Certo"),
        "neutral": ("{name}. ", "Guarda, {name}. ", "Allora, {name}. ", "Ecco, {name}. "),
        "gratitude": ("Prego", "Con piacere", "Felice di aiutarti", "Quando vuoi"),
        "farewell": ("A presto", "Resto a disposizione", "Ciao", "Stammi bene"),
        "reassurance": ("Nessun problema", "Va tutto bene", "Non preoccuparti", "Sistemiamo tutto"),
        "greeting": {"general":"Ciao", "morning":"Buongiorno", "day":"Buongiorno", "evening":"Buonasera"},
    },
    "pt": {
        "action": ("Entendido", "Certo", "Combinado", "Já trato disso", "Pode deixar", "É para já", "Claro"),
        "neutral": ("{name}. ", "Olha, {name}. ", "Então, {name}. ", "Aqui está, {name}. "),
        "gratitude": ("De nada", "Com prazer", "Fico feliz em ajudar", "Sempre às ordens"),
        "farewell": ("Até logo", "Estarei por aqui", "Tchau", "Cuida-te"),
        "reassurance": ("Sem problema", "Está tudo bem", "Não se preocupe", "Vamos resolver"),
        "greeting": {"general":"Olá", "morning":"Bom dia", "day":"Boa tarde", "evening":"Boa noite"},
    },
}


def address_prefix(context: AddressContext | str, code: str, name: str, counter: int = 0) -> str:
    code = _code(code)
    ctx = context if isinstance(context, AddressContext) else AddressContext(kind=str(context or "action"))
    clean_name = str(name or "").strip()
    if not clean_name or ctx.kind == "self_addressed":
        return ""
    profile = _PREFIXES[code]
    kind = ctx.kind if ctx.kind in profile else "neutral"
    if kind == "neutral":
        neutral = profile["neutral"]
        if isinstance(neutral, tuple):        # NEO 183: several natural forms, rotated
            neutral = neutral[int(counter or 0) % len(neutral)]
        return str(neutral).format(name=clean_name)
    if kind == "greeting":
        phrase = profile["greeting"].get(ctx.greeting, profile["greeting"]["general"])
        return f"{phrase}, {clean_name}. "
    choices = profile[kind]
    if isinstance(choices, tuple):
        phrase = choices[int(counter or 0) % len(choices)]
    else:
        phrase = str(choices)
    return f"{phrase}, {clean_name}. "


def merge_social_address160(body: str, prefix: str) -> str:
    """Insert the configured name without echoing a complete salutation.

    The offline replies must stand alone when personal addressing is disabled.
    Only a separate greeting token is replaced; action results and named data
    are never stripped based on a substring match.
    """
    for code, profile in _PREFIXES.items():
        starts = tuple(profile['greeting'].values())
        if not any(prefix.casefold().startswith(p.casefold() + ', ') for p in starts):
            continue
        aliases = {
            'ru': ('Привет', 'Здравствуй', 'Здравствуйте'),
            'en': ('Hi', 'Hello', 'Hey'),
            'de': ('Hallo', 'Guten Tag'),
            'fr': ('Salut', 'Bonjour', 'Bonsoir'),
            'es': ('Hola',), 'it': ('Ciao', 'Salve'), 'pt': ('Olá', 'Oi'),
        }[code]
        pattern = r'^\s*[¡¿]?(?:' + '|'.join(re.escape(p) for p in (*starts, *aliases)) + r')\s*[!.:]\s*'
        matched = re.match(pattern, body, re.I)
        if matched:
            return (prefix + body[matched.end():]).rstrip()
    # REC-WORDING-SOURCE-01 W03: «Пожалуйста, Анна. Пожалуйста! Рад…» — the name
    # goes into the single act of thanks; the body's own opener is dropped.
    for code, profile in _PREFIXES.items():
        starts = tuple(profile.get('gratitude', ()) or ())
        folded = prefix.replace('’', "'").casefold()
        if not starts or not any(folded.startswith(p.replace('’', "'").casefold() + ', ') for p in starts):
            continue
        openers = (*starts, *_THANKS_OPENERS187.get(code, ()))
        pattern = r'^\s*[¡]?(?:' + '|'.join(re.escape(p.replace('’', "'")) for p in openers) + r')\s*[!.]\s*'
        matched = re.match(pattern, body.replace('’', "'"), re.I)
        if matched:
            return (prefix + body[matched.end():]).rstrip()
    return (prefix + body).rstrip()


_THANKS_OPENERS187 = {
    'ru': ('Пожалуйста', 'Не за что', 'Всегда пожалуйста', 'Рад помочь', 'На здоровье'),
    'en': ("You're welcome", 'No problem', 'Happy to help', 'Glad I could help'),
    'de': ('Gern geschehen', 'Kein Problem', 'Immer gern', 'Gern'),
    'fr': ('Avec plaisir', 'Pas de souci', "Je t'en prie", "Content d'avoir pu aider"),
    'es': ('De nada', 'No hay de qué', 'Con gusto', 'Encantado de ayudar'),
    'it': ('Prego', 'Di nulla', 'Figurati', 'Volentieri'),
    'pt': ('De nada', 'Não tens de quê', 'Com gosto', 'Sempre às ordens'),
}


def fast_social_reply(text: str, code: str) -> str | None:
    """Complete fallback replies, also when personal addressing is disabled.

    ``merge_social_address160`` handles the optional name without making the
    salutation depend on that setting. A command tail never becomes small talk.
    """
    code = _code(code)
    value = _norm(text)
    p = _SOCIAL[code]
    kind, _sub = social_kind(text, code)
    how = any(_norm(x) in value for x in p["how_are_you"])
    if kind == "greeting":
        # Greeting may prefix a real request ("привет, открой браузер"). In that
        # case it owns only the address style; it must never swallow the command.
        matched = ""
        for bucket in ("greeting_morning", "greeting_day", "greeting_evening", "greeting_general"):
            matched = _starts_with_phrase(value, p[bucket])
            if matched:
                break
        tail = value[len(matched):].strip() if matched else value
        filler_words = {
            "ru": ("нео", "нио", "neo", "джарвис", "джервис", "jarvis", "пожалуйста"),
            "en": ("neo", "jarvis", "please"), "de": ("neo", "jarvis", "bitte"),
            "fr": ("neo", "néo", "jarvis"), "es": ("neo", "jarvis"), "it": ("neo", "jarvis"), "pt": ("neo", "jarvis"),
        }[code]
        for filler in filler_words:
            tail = re.sub(rf"(?:^|\s){re.escape(_norm(filler))}(?=\s|$)", " ", tail)
        tail = re.sub(r"\s+", " ", tail).strip()
        pure_how = any(tail == _norm(x) for x in p["how_are_you"])
        if tail and not pure_how:
            return None
        greeting = _PREFIXES[code]['greeting'].get(_sub, _PREFIXES[code]['greeting']['general']) + '! '
        if how:
            return greeting + {
                "ru":"Хорошо, спасибо! Рад разговору. Как у тебя дела?",
                "en":"Good, thanks! It’s nice to chat. How are you doing?",
                "de":"Gut, danke! Schön, mit dir zu reden. Wie geht es dir?",
                "fr":"Bien, merci ! Ça fait plaisir de discuter. Et toi, comment vas-tu ?",
                "es":"¡Bien, gracias! Me alegra charlar contigo. ¿Tú cómo estás?",
                "it":"Bene, grazie! Mi fa piacere parlare con te. Tu come stai?",
                "pt":"Bem, obrigado! É bom conversar contigo. E tu, como estás?",
            }[code]
        return greeting + {
            "ru":"Чем могу помочь?", "en":"How can I help?", "de":"Wie kann ich helfen?",
            "fr":"Comment puis-je aider ?", "es":"¿En qué puedo ayudar?", "it":"Come posso aiutarti?",
            "pt":"Como posso ajudar?",
        }[code]
    if kind == "gratitude":
        return {
            "ru":"Рад помочь.", "en":"Glad to help.", "de":"Gern geholfen.", "fr":"Avec plaisir.",
            "es":"Encantado de ayudar.", "it":"Felice di aiutarti.", "pt":"Fico feliz em ajudar.",
        }[code]
    if kind == "farewell":
        return {
            "ru":"Если понадоблюсь — я рядом.", "en":"I'll be here if you need me.",
            "de":"Ich bin da, wenn du mich brauchst.", "fr":"Je suis là si tu as besoin de moi.",
            "es":"Aquí estaré si me necesitas.", "it":"Sono qui se ti servo.",
            "pt":"Estarei por aqui se precisar de mim.",
        }[code]
    if kind == "reassurance":
        return {
            "ru":"Продолжаем.", "en":"We can continue.", "de":"Wir können weitermachen.",
            "fr":"On peut continuer.", "es":"Podemos continuar.", "it":"Possiamo continuare.",
            "pt":"Podemos continuar.",
        }[code]

    if any(value == _norm(x) for x in p["how_are_you"]):
        return {
            "ru":"Всё работает, я на связи. Чем помочь?", "en":"I'm doing well and I'm here. How can I help?",
            "de":"Alles läuft, ich bin da. Wie kann ich helfen?", "fr":"Tout va bien, je suis là. Comment puis-je aider ?",
            "es":"Todo va bien, estoy aquí. ¿En qué puedo ayudar?", "it":"Tutto bene, sono qui. Come posso aiutarti?",
            "pt":"Está tudo bem, estou por aqui. Como posso ajudar?",
        }[code]
    if any(value == _norm(x) for x in p["presence"]):
        return {
            "ru":"Да, я здесь.", "en":"Yes, I'm here.", "de":"Ja, ich bin da.", "fr":"Oui, je suis là.",
            "es":"Sí, estoy aquí.", "it":"Sì, sono qui.", "pt":"Sim, estou aqui.",
        }[code]
    return None


def atlas_response_style(action: str) -> str:
    """Public helper used by regression/audit tooling for every Atlas row."""
    value = str(action or "").strip().casefold()
    if not value:
        return "neutral"
    return "neutral" if value in _INFORMATION_ACTIONS else "action"
