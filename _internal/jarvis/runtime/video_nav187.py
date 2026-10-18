"""NEO 187 HREQ-026: «следующее видео» / «предыдущее видео» in seven languages.

This is navigation to ANOTHER video, never a seek inside the current one and
never music. The phrase must be a complete command («включи следующее видео»);
anything with extra content («следующее видео про котов») keeps its old route.
Execution goes through the media tool, which uses the verified background
Windows media session (no mouse or keyboard input) and reports honestly when
YouTube offers no previous/next video.
"""
from __future__ import annotations

import re
from functools import lru_cache

from jarvis.runtime.utterance_purpose import strip_assistant_address

_NEXT = r"следующ\w*|next|nächste\w*|naechste\w*|suivante?|prochaine?|siguiente|próximo|proximo|successivo|seguente|prossimo|seguinte"
_PREV = r"предыдущ\w*|прошл\w*|прежн\w*|previous|prior|vorherige\w*|vorige\w*|précédente?|precedente?|anterior"
_NOUN = {
    "ru": r"(?:видео|видос\w*|ролик\w*|клип\w*)(?:\s+(?:на|в)\s+(?:ютубе?|youtube))?",
    "en": r"(?:youtube\s+)?(?:video|clip)(?:\s+on\s+youtube)?",
    "de": r"(?:youtube-?)?video(?:\s+auf\s+youtube)?",
    "fr": r"(?:vidéo|video)(?:\s+(?:sur\s+)?youtube)?",
    "es": r"(?:vídeo|video)(?:\s+(?:de|en)\s+youtube)?",
    "it": r"video(?:\s+(?:di|su)\s+youtube)?",
    "pt": r"(?:vídeo|video)(?:\s+(?:do|no)\s+youtube)?",
}
_VERB = {
    "ru": r"(?:включи(?:те)?|поставь(?:те)?|запусти(?:те)?|покажи(?:те)?|давай|открой(?:те)?|переключи(?:сь|те)?(?:\s+на)?|перейди(?:те)?(?:\s+(?:на|к|ко))?|вернись(?:\s+(?:на|к|ко))?|верни(?:те)?|назад\s+к|хочу)",
    "en": r"(?:play|put\s+on|show|open|go\s+to|skip\s+to|switch\s+to|go\s+back\s+to|back\s+to|return\s+to)",
    "de": r"(?:spiel(?:e)?|zeig(?:e)?|starte|geh(?:e)?\s+zu(?:m|r)?|wechsle\s+zu(?:m|r)?|zurück\s+zu(?:m|r)?|zurueck\s+zu(?:m|r)?)",
    "fr": r"(?:mets|lance|joue|montre|passe\s+(?:à|a)|reviens\s+(?:à|a)|va\s+(?:à|a)|retourne\s+(?:à|a))",
    "es": r"(?:pon|reproduce|muestra|pasa\s+(?:a|al)|vuelve\s+(?:a|al)|ve\s+(?:a|al)|ir\s+(?:a|al))",
    "it": r"(?:metti|riproduci|mostra|passa\s+(?:a|al)|torna\s+(?:a|al)|vai\s+(?:a|al))",
    "pt": r"(?:põe|poe|ponha|reproduz|reproduza|toca|toque|mostra|mostre|passa\s+(?:para|ao)|volta\s+(?:para|a|ao)|volte\s+(?:para|a|ao)|vai\s+(?:para|ao))",
}
_ART = {
    "ru": r"(?:к\s+|на\s+)?", "en": r"(?:the\s+)?", "de": r"(?:das\s+|dem\s+|zum\s+)?",
    "fr": r"(?:la\s+)?", "es": r"(?:el\s+|la\s+)?", "it": r"(?:il\s+|lo\s+)?",
    "pt": r"(?:o\s+)?",
}
_SKIP = {
    "ru": r"(?:пропусти|скипни|пролистай)(?:\s+(?:это|этот|текущее|текущий))?\s+(?:видео|ролик)",
    "en": r"skip\s+(?:this|the|current)?\s*(?:video|clip)",
    "de": r"überspring(?:e)?\s+(?:das|dieses)?\s*video",
    "fr": r"passe\s+(?:cette|la)\s+vidéo",
    "es": r"salta(?:te)?\s+(?:este|el)\s+(?:vídeo|video)",
    "it": r"salta\s+(?:questo|il)\s+video",
    "pt": r"salta\s+(?:este|o)\s+(?:vídeo|video)",
}
_POLITE = r"(?:пожалуйста|please|bitte|s'il te plaît|s'il te plait|por favor|per favore)"


@lru_cache(maxsize=8)
def _patterns(code: str):
    noun, verb, art = _NOUN[code], _VERB[code], _ART[code]
    adj = r"(?P<adj>" + _NEXT + "|" + _PREV + r")"
    adj2 = r"(?P<adj2>" + _NEXT + "|" + _PREV + r")"
    head = r"^(?:" + verb + r"\s+)?" + art
    return (
        re.compile(head + adj + r"\s+" + noun + r"(?:\s+(?:de|en|di|su|do|no|on|auf|sur|на|в)\s+(?:youtube|ютубе?))?$", re.I),
        # NEO 188 atlas: «vuelve al vídeo anterior de YouTube» — the service may follow the adjective.
        re.compile(head + noun + r"\s+" + adj2 + r"(?:\s+(?:de|en|di|su|do|no|on|auf|sur|на|в)\s+(?:youtube|ютубе?))?$", re.I),
        re.compile(r"^" + _SKIP[code] + r"$", re.I),
    )


def parse(text: str, code: str, *, names=()) -> str | None:
    """'next' / 'previous' for a complete video-navigation command, else None."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 160 or any(c in raw for c in '\n\r:;"«»“”„`'):
        return None
    if code not in _NOUN:
        return None
    value = " ".join(raw.casefold().replace("ё", "е").replace("’", "'").split()).strip(" ,.!?")
    value = strip_assistant_address(value, names).strip(" ,.!?")
    value = re.sub(r"^" + _POLITE + r"[\s,]+|[\s,]+" + _POLITE + r"$", "", value).strip(" ,.!?")
    for pattern in _patterns(code):
        match = pattern.fullmatch(value)
        if not match:
            continue
        word = (match.groupdict().get("adj") or match.groupdict().get("adj2") or "")
        if not word:
            return "next"  # «пропусти видео»
        return "previous" if re.fullmatch(_PREV, word, re.I) else "next"
    return None


_TEXT = {
    "next_done": ("Включил следующее видео YouTube: {title}.", "Playing the next YouTube video: {title}.",
                  "Das nächste YouTube-Video läuft: {title}.", "Vidéo YouTube suivante : {title}.",
                  "Siguiente vídeo de YouTube: {title}.", "Video successivo di YouTube: {title}.",
                  "Próximo vídeo do YouTube: {title}."),
    "previous_done": ("Вернулся к предыдущему видео YouTube: {title}.", "Back to the previous YouTube video: {title}.",
                      "Zurück zum vorherigen YouTube-Video: {title}.", "Retour à la vidéo YouTube précédente : {title}.",
                      "Vuelvo al vídeo anterior de YouTube: {title}.", "Torno al video precedente di YouTube: {title}.",
                      "Voltei ao vídeo anterior do YouTube: {title}."),
    "next_unavailable": ("Следующее видео недоступно: YouTube сейчас не предлагает переход вперёд.",
                         "The next video isn’t available: YouTube doesn’t offer a next video right now.",
                         "Kein nächstes Video verfügbar: YouTube bietet gerade keinen Wechsel nach vorn an.",
                         "Pas de vidéo suivante : YouTube ne propose pas de vidéo suivante pour l’instant.",
                         "No hay vídeo siguiente: YouTube no ofrece pasar al siguiente ahora mismo.",
                         "Nessun video successivo: YouTube al momento non offre il passaggio avanti.",
                         "Não há próximo vídeo: o YouTube não oferece avançar neste momento."),
    "previous_unavailable": ("Предыдущее видео недоступно: YouTube не предлагает переход назад (обычно он есть только в плейлисте).",
                             "The previous video isn’t available: YouTube offers no previous video here (usually only inside a playlist).",
                             "Kein vorheriges Video verfügbar: YouTube bietet hier keinen Schritt zurück an (meist nur in einer Playlist).",
                             "Pas de vidéo précédente : YouTube n’en propose pas ici (en général seulement dans une playlist).",
                             "No hay vídeo anterior: YouTube no lo ofrece aquí (normalmente solo dentro de una lista).",
                             "Nessun video precedente: YouTube qui non lo offre (di solito solo in una playlist).",
                             "Não há vídeo anterior: o YouTube não o oferece aqui (normalmente só numa playlist)."),
    "unverified": ("Команду перехода отправил, но YouTube не подтвердил смену видео.",
                   "I sent the command, but YouTube didn’t confirm that the video changed.",
                   "Der Befehl wurde gesendet, aber YouTube hat den Videowechsel nicht bestätigt.",
                   "J’ai envoyé la commande, mais YouTube n’a pas confirmé le changement de vidéo.",
                   "Envié la orden, pero YouTube no confirmó el cambio de vídeo.",
                   "Ho inviato il comando, ma YouTube non ha confermato il cambio di video.",
                   "Enviei o comando, mas o YouTube não confirmou a mudança de vídeo."),
    "no_session": ("Не нашёл открытое видео YouTube, которым можно управлять.",
                   "I couldn’t find an open YouTube video I can control.",
                   "Ich habe kein offenes, steuerbares YouTube-Video gefunden.",
                   "Je n’ai trouvé aucune vidéo YouTube ouverte que je peux contrôler.",
                   "No encontré ningún vídeo de YouTube abierto que pueda controlar.",
                   "Non ho trovato un video YouTube aperto che posso controllare.",
                   "Não encontrei nenhum vídeo do YouTube aberto que eu possa controlar."),
}
_ORDER = ("ru", "en", "de", "fr", "es", "it", "pt")


def text(key: str, code: str, **values) -> str:
    row = _TEXT[key]
    sentence = row[_ORDER.index(code) if code in _ORDER else 0]
    title = str(values.get("title", "") or "").strip()
    if "{title}" in sentence and not title:
        sentence = sentence.replace(": {title}.", ".").replace(" : {title}.", ".")
    return sentence.format(title=title[:160])


def handle(agent, raw_text: str, code: str):
    """Run the media tool for a complete video-navigation command, else None."""
    config = getattr(agent, "config", None)
    names = ()
    if config is not None:
        names = (config.get("assistant_name", "NEO"), config.get("wake_word.phrase", "") or "")
    action = parse(raw_text, code, names=names)
    if action is None:
        return None
    from jarvis.routing.ownership_firewall import bind_exact_tool_ownership
    from jarvis.runtime.router import RoutedIntent
    bind_exact_tool_ownership("media", text=raw_text)
    intent = RoutedIntent("youtube_" + action, "media", {"action": action, "target": "youtube"}, 946, 1.0,
                          ("NEO 187 HREQ-026", "только видео", "не музыка"))
    return agent._execute_routed_intent(intent, raw_text)
