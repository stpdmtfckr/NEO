"""Typed music transport/mode grammar for the seven supported languages.

MEDIA185-U03: word-by-word translation turned «Pasa a la canción siguiente»
into a pause, «Take this song off pause» into a pause, «Metti in loop tutta la
playlist» into a playlist search and «Go back to the last song» into a
translation. The whole native action+object phrase is recognised here first and
mapped to one canonical internal command; anything that is not a complete
transport/mode phrase returns None and keeps its usual route (playlist titles,
search payloads, chat). Negated, quoted or dictated text never reaches this
grammar (the caller checks negation first and quoted text cannot fullmatch).
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Canonical internal commands (already routed by the Russian surface).
NEXT, PREVIOUS, RESUME, PAUSE, STOP = "следующий трек", "предыдущий трек", "продолжи музыку", "поставь музыку на паузу", "останови музыку"
SHUFFLE_ON, SHUFFLE_OFF = "включи перемешивание", "выключи перемешивание"
REPEAT_TRACK, REPEAT_PLAYLIST, REPEAT_OFF = "включи повтор трека", "включи повтор плейлиста", "выключи повтор"
QUEUE = "покажи очередь"

_SPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("’", "'").replace("ё", "е")
    value = re.sub(r"[!?.,:;¡¿]+", " ", value)
    return _SPACE.sub(" ", value).strip()


_POLITE = {
    "ru": r"(?:пожалуйста|плиз|будь добр|будь любезен|если можно)",
    "en": r"(?:please|pls|now|for me|thanks|thank you)",
    "de": r"(?:bitte|mal|jetzt|danke)",
    "fr": r"(?:s'il te plait|s'il te plaît|s'il vous plait|s'il vous plaît|stp|svp|merci|maintenant)",
    "es": r"(?:por favor|porfa|ahora|gracias)",
    "it": r"(?:per favore|per piacere|perfavore|grazie|adesso|ora)",
    "pt": r"(?:por favor|se faz favor|agora|obrigado|obrigada)",
}
_LEAD = {
    "ru": r"(?:(?:а|ну|слушай|давай|можешь|можно|пожалуйста)\s+)*",
    "en": r"(?:(?:hey|ok|okay|so|now|please|can you|could you|would you|will you)\s+)*",
    "de": r"(?:(?:hey|ok|okay|also|jetzt|bitte|kannst du|könntest du)\s+)*",
    "fr": r"(?:(?:hey|ok|bon|alors|maintenant|peux-tu|tu peux|pourrais-tu|s'il te plait|s'il te plaît)\s+)*",
    "es": r"(?:(?:oye|ok|vale|bueno|ahora|por favor|puedes|podrías|podrias)\s+)*",
    "it": r"(?:(?:ehi|ok|allora|adesso|ora|per favore|puoi|potresti)\s+)*",
    "pt": r"(?:(?:ei|ok|bem|agora|por favor|podes|pode|poderias|você pode|voce pode)\s+)*",
}
# Source qualifiers, kept separate from the action and turned into a suffix.
_SOURCES = {
    "yandex": r"(?:(?:в|во|на|через|in|on|im|auf|in der|sur|dans|en|su|no|na|em)\s+)?(?:яндекс(?:\s+музык\w*)?|yandex(?:\s+(?:music|musik|musique|música|musica))?)",
    "spotify": r"(?:(?:в|на|через|in|on|im|auf|sur|dans|en|su|no|na|em)\s+)?(?:spotify|спотифа[йя]\w*|спотик\w*)",
    "discord": r"(?:(?:в|на|через|in|on|im|auf|sur|dans|en|su|no|na|em)\s+)?(?:discord|дискорд\w*)",
    "youtube": r"(?:(?:в|на|через|in|on|im|auf|sur|dans|en|su|no|na|em)\s+)?(?:youtube|ютуб\w*)",
    "pc": r"(?:(?:на|в|on|on the|auf dem|am|sur l'|sur le|en el|en la|sul|no|na)\s+)?(?:пк|компьютер\w*|pc|computer|rechner|ordinateur|ordi|computadora|ordenador|computador)",
}
_SUFFIX = {"yandex": " в яндекс музыке", "spotify": " в спотифай", "discord": " в дискорде", "youtube": "", "pc": ""}

_SONG = {
    "en": r"(?:song|track|tune|one)",
    "de": r"(?:lied|titel|song|track|stück|stueck)",
    "fr": r"(?:chanson|morceau|titre|piste|son)",
    "es": r"(?:canción|cancion|tema|pista)",
    "it": r"(?:canzone|brano|pezzo|traccia)",
    "pt": r"(?:música|musica|faixa|canção|cancao)",
}
_PLAYLIST = {
    "en": r"(?:playlist|queue)", "de": r"(?:playlist|wiedergabeliste|warteschlange)",
    "fr": r"(?:playlist|liste de lecture|file d'attente|file)", "es": r"(?:playlist|lista(?: de reproducción| de reproduccion)?|cola)",
    "it": r"(?:playlist|scaletta|coda)", "pt": r"(?:playlist|lista(?: de reprodução| de reproducao)?|fila)",
}


def _rules(code: str) -> list[tuple[str, str]]:
    s = _SONG.get(code, "")
    p = _PLAYLIST.get(code, "")
    art = {
        "en": r"(?:the |this |my |that )?", "de": r"(?:den |das |die |dem |der |dieses |diesen |mein |meine )?",
        "fr": r"(?:la |le |les |l'|ce |cette |ma |mon )?", "es": r"(?:la |el |esta |este |mi )?",
        "it": r"(?:la |il |lo |l'|questa |questo |tutta la |tutto il )?", "pt": r"(?:a |o |esta |este |essa |esse |minha |meu )?",
    }.get(code, "")
    if code == "en":
        return [
            (rf"(?:play |go to |skip to |move on to |jump to )?(?:the )?next {s}|next|skip(?: this| the)?(?: {s})?|skip ahead", NEXT),
            (rf"(?:play |go back to |back to |return to )?(?:the )?(?:previous|last|prior) {s}|go back one {s}", PREVIOUS),
            (rf"(?:resume|unpause|continue|keep playing|continue playing)(?: {art}(?:music|{s}|playback))?|take {art}(?:music|{s}) off pause|play {art}(?:music|{s}) again", RESUME),
            (rf"pause(?: {art}(?:music|{s}|playback))?|put {art}(?:music|{s}) on pause", PAUSE),
            (rf"stop(?: {art}(?:music|{s}|playback|playing))?", STOP),
            (rf"(?:turn on|enable|start|switch on) shuffle|shuffle(?: on| {art}(?:{p}|music|songs|tracks))?|shuffle mode on", SHUFFLE_ON),
            (r"(?:turn off|disable|stop|switch off) shuffl(?:e|ing)|shuffle off|no shuffle", SHUFFLE_OFF),
            (rf"(?:repeat|loop)(?: {art}(?:current )?{s})|(?:turn on|enable) (?:repeat|loop)(?: for)? {art}(?:current )?{s}|repeat one|repeat track", REPEAT_TRACK),
            (rf"(?:repeat|loop)(?: {art}(?:whole |entire |full )?{p})|(?:turn on|enable) (?:repeat|loop)(?: for)? {art}(?:whole )?{p}|repeat playlist|repeat all", REPEAT_PLAYLIST),
            (r"(?:turn off|disable|stop|switch off) (?:repeat|repeating|looping|the loop)|repeat off|no repeat", REPEAT_OFF),
            (rf"(?:show(?: me)?|open|display|what's in|what is in|list) {art}{p}|what's next|what is next|what's up next|what plays next", QUEUE),
        ]
    if code == "de":
        return [
            (rf"(?:spiel(?:e)? )?(?:den |das )?(?:nächsten|nächste|naechsten|naechste) {s}|(?:zum |zu dem )?(?:nächsten|nächste) {s}|nächster titel|nächstes lied|weiter|überspring(?:e)?(?: {art}{s})?|spring(?:e)? (?:zum|auf den) nächsten(?: {s})?", NEXT),
            (rf"(?:spiel(?:e)? )?(?:den |das )?(?:vorherigen|vorherige|letzten|letzte) {s}|vorheriger titel|vorheriges lied|zurück zum (?:vorherigen|letzten)(?: {s})?", PREVIOUS),
            (rf"setze? {art}(?:musik|{s}|wiedergabe) fort|(?:musik|wiedergabe) fortsetzen|fortsetzen|weiterspielen|spiel(?:e)? weiter|mach weiter(?: mit der musik)?", RESUME),
            (rf"pausier(?:e)?(?: {art}(?:musik|{s}|wiedergabe))?|pause(?: {art}(?:musik|{s}))?|(?:musik|wiedergabe) pausieren", PAUSE),
            (rf"stopp(?:e)?(?: {art}(?:musik|{s}|wiedergabe))?|halt(?:e)? {art}(?:musik|wiedergabe) an|(?:musik|wiedergabe) stoppen", STOP),
            (rf"(?:schalte |mach |aktiviere )?(?:die )?zufallswiedergabe (?:an|ein)|zufallswiedergabe|aktiviere (?:die )?zufallswiedergabe|misch(?:e)? {art}(?:{p}|musik|lieder)|shuffle(?: an)?", SHUFFLE_ON),
            (r"(?:schalte |mach |deaktiviere )?(?:die )?zufallswiedergabe (?:aus|ab)|deaktiviere (?:die )?zufallswiedergabe|shuffle aus", SHUFFLE_OFF),
            (rf"wiederhol(?:e)? {art}(?:aktuellen |aktuelle )?{s}|{s} wiederholen|(?:schalte )?(?:die )?wiederholung (?:für|fuer) {art}{s}(?: an| ein)?", REPEAT_TRACK),
            (rf"wiederhol(?:e)? {art}(?:ganze |gesamte )?{p}|{p} wiederholen|(?:schalte )?(?:die )?wiederholung (?:für|fuer) {art}{p}(?: an| ein)?|{p} in (?:der )?schleife", REPEAT_PLAYLIST),
            (r"(?:schalte |mach )?(?:die )?wiederholung (?:aus|ab)|deaktiviere (?:die )?wiederholung|nicht mehr wiederholen", REPEAT_OFF),
            (rf"zeig(?:e)?(?: mir)? {art}{p}|was kommt als nächstes|was läuft als nächstes", QUEUE),
        ]
    if code == "fr":
        return [
            (rf"(?:passe|passer|va|aller) (?:à |a )?(?:la |le )?(?:{s} )?suivant(?:e)?|(?:la |le )?{s} suivant(?:e)?|suivant(?:e)?|(?:joue )?(?:la |le )?(?:{s} )?suivant(?:e)?|saute(?: {art}{s})?", NEXT),
            (rf"(?:reviens|revenir|retourne|retour) (?:à |a )?(?:la |le )?(?:{s} )?(?:précédent(?:e)?|precedent(?:e)?)|(?:la |le )?{s} (?:précédent(?:e)?|precedent(?:e)?)|(?:précédent(?:e)?|precedent(?:e)?)", PREVIOUS),
            (rf"reprends?(?: {art}(?:musique|lecture|{s}))?|reprendre(?: {art}(?:musique|lecture))?|continue(?: {art}(?:musique|lecture))?|enlève la pause|enleve la pause|relance {art}(?:musique|{s})", RESUME),
            (rf"mets? {art}(?:musique|{s}|lecture) en pause|pause(?: {art}(?:musique|{s}))?", PAUSE),
            (rf"(?:arrête|arrete|stoppe|stop)(?: {art}(?:musique|lecture|{s}))?", STOP),
            (rf"(?:active|allume|mets) (?:la )?lecture (?:aléatoire|aleatoire)|lecture (?:aléatoire|aleatoire)(?: activée)?|mélange {art}(?:{p}|musique|morceaux)|melange {art}(?:{p}|musique)|shuffle", SHUFFLE_ON),
            (r"(?:désactive|desactive|coupe|arrête|arrete|enlève|enleve) (?:la )?lecture (?:aléatoire|aleatoire)", SHUFFLE_OFF),
            (rf"(?:répète|repete|répéter|repeter) {art}{s}|mets {art}{s} en boucle|{s} en boucle", REPEAT_TRACK),
            (rf"(?:répète|repete|répéter|repeter) {art}(?:toute la )?{p}|mets {art}(?:toute la )?{p} en boucle|{p} en boucle", REPEAT_PLAYLIST),
            (r"(?:désactive|desactive|coupe|arrête|arrete|enlève|enleve) (?:la )?(?:répétition|repetition|boucle)", REPEAT_OFF),
            (rf"(?:montre|affiche)(?:-moi| moi)? {art}{p}|qu'est-ce qui (?:suit|vient ensuite)|c'est quoi la suite", QUEUE),
        ]
    if code == "es":
        return [
            (rf"(?:pasa|pasar|ve|ir|salta|saltar|cambia) (?:a )?(?:la |el )?(?:{s} )?siguiente|(?:pon |reproduce )?(?:la |el )?(?:siguiente {s}|{s} siguiente)|siguiente|salta(?: {art}{s})?|adelante", NEXT),
            (rf"(?:vuelve|volver|regresa|ve) (?:a )?(?:la |el )?(?:{s} )?anterior|(?:pon |reproduce )?(?:la |el )?(?:{s} anterior|anterior {s})|anterior", PREVIOUS),
            (rf"reanuda(?:r)?(?: {art}(?:música|musica|{s}|reproducción|reproduccion))?|continúa(?: {art}(?:música|musica|reproducción|reproduccion))?|continua(?: {art}(?:música|musica))?|quita(?:le)? la pausa|sigue (?:la música|la musica|reproduciendo)", RESUME),
            (rf"pausa(?: {art}(?:música|musica|{s}))?|pon {art}(?:música|musica|{s}) en pausa|pausar(?: {art}(?:música|musica))?", PAUSE),
            (rf"(?:para|detén|deten|detener|parar)(?: {art}(?:música|musica|{s}|reproducción|reproduccion))?", STOP),
            (rf"(?:activa|enciende|pon) (?:el )?(?:modo aleatorio|aleatorio|reproducción aleatoria|reproduccion aleatoria)|mezcla {art}(?:{p}|música|musica|canciones)|modo aleatorio|reproducción aleatoria|reproduccion aleatoria|shuffle", SHUFFLE_ON),
            (r"(?:desactiva|apaga|quita) (?:el )?(?:modo aleatorio|aleatorio|reproducción aleatoria|reproduccion aleatoria)", SHUFFLE_OFF),
            (rf"repite {art}{s}|pon {art}{s} en (?:bucle|repetición|repeticion)|{s} en bucle", REPEAT_TRACK),
            (rf"repite {art}(?:toda la )?{p}|pon {art}(?:toda la )?{p} en (?:bucle|repetición|repeticion)|{p} en bucle", REPEAT_PLAYLIST),
            (r"(?:desactiva|apaga|quita) (?:la )?(?:repetición|repeticion|el bucle)|no repitas", REPEAT_OFF),
            (rf"(?:muéstrame|muestrame|enséñame|enseñame|ensename|muestra) {art}{p}|qué sigue|que sigue|qué viene después|que viene despues", QUEUE),
        ]
    if code == "it":
        return [
            (rf"(?:passa|vai|salta) (?:alla |al )?(?:{s} )?successiv[oa]|(?:metti |riproduci )?(?:la |il )?(?:prossim[oa] {s}|{s} successiv[oa])|prossim[oa]|successiv[oa]|salta(?: {art}{s})?|avanti", NEXT),
            (rf"(?:torna|ritorna) (?:alla |al )?(?:{s} )?precedente|(?:la |il )?{s} precedente|precedente|indietro", PREVIOUS),
            (rf"riprendi(?: {art}(?:musica|{s}|riproduzione))?|continua(?: {art}(?:musica|riproduzione))?|togli la pausa", RESUME),
            (rf"(?:metti in )?pausa(?: {art}(?:musica|{s}))?|metti {art}(?:musica|{s}) in pausa", PAUSE),
            (rf"(?:ferma|stop|interrompi)(?: {art}(?:musica|{s}|riproduzione))?", STOP),
            (rf"(?:attiva|accendi|metti) (?:la )?riproduzione casuale|riproduzione casuale|mescola {art}(?:{p}|musica|canzoni)|shuffle", SHUFFLE_ON),
            (r"(?:disattiva|spegni|togli) (?:la )?riproduzione casuale", SHUFFLE_OFF),
            (rf"ripeti {art}{s}|metti {art}{s} in loop|{s} in loop", REPEAT_TRACK),
            (rf"ripeti {art}{p}|metti in loop {art}(?:tutta la )?{p}|metti {art}(?:tutta la )?{p} in loop|{p} in loop", REPEAT_PLAYLIST),
            (r"(?:disattiva|spegni|togli) (?:la )?(?:ripetizione|il loop)", REPEAT_OFF),
            (rf"(?:mostrami|mostra|fammi vedere) {art}{p}|cosa c'è in {p}|cosa c'e in {p}|cosa viene dopo", QUEUE),
        ]
    if code == "pt":
        return [
            (rf"(?:passe|passa|pula|salta|avança|avanca|vai|vá) (?:para |pra )?(?:a |o )?(?:próxim[ao]|proxim[ao])(?: {s})?|(?:toca |põe |poe )?(?:a |o )?(?:próxim[ao]|proxim[ao]) {s}|(?:próxim[ao]|proxim[ao])|pula(?: {art}{s})?|avança|avanca", NEXT),
            (rf"(?:volta|volte|regressa) (?:para |pra )?(?:a |o )?{s} anterior|(?:a |o )?{s} anterior|anterior", PREVIOUS),
            (rf"(?:retoma|retome|continua|continue)(?: {art}(?:música|musica|{s}|reprodução|reproducao))?|tira a pausa|tire a pausa", RESUME),
            (rf"pausa(?: {art}(?:música|musica|{s}))?|(?:põe|poe|coloca) {art}(?:música|musica|{s}) em pausa|pausar", PAUSE),
            (rf"(?:para|pare|parar)(?: {art}(?:música|musica|{s}|reprodução|reproducao))?", STOP),
            (rf"(?:ativa|ative|liga|ligue) (?:o )?(?:modo aleatório|modo aleatorio|aleatório|aleatorio|reprodução aleatória|reproducao aleatoria)|embaralha {art}(?:{p}|música|musica|músicas)|modo aleatório|reprodução aleatória|shuffle", SHUFFLE_ON),
            (r"(?:desativa|desative|desliga|desligue) (?:o )?(?:modo aleatório|modo aleatorio|aleatório|aleatorio|reprodução aleatória|reproducao aleatoria)", SHUFFLE_OFF),
            (rf"repete {art}{s}|repita {art}{s}|(?:coloca|põe|poe) {art}{s} em (?:loop|repetição|repeticao)", REPEAT_TRACK),
            (rf"repete {art}(?:toda a )?{p}|repita {art}(?:toda a )?{p}|(?:coloca|põe|poe) {art}(?:toda a )?{p} em (?:loop|repetição|repeticao)", REPEAT_PLAYLIST),
            (r"(?:desativa|desative|desliga|desligue) (?:a )?(?:repetição|repeticao|o loop)", REPEAT_OFF),
            (rf"(?:mostra|mostre|mostra-me|mostre-me)(?: me)? {art}{p}|o que vem a seguir|o que vem depois", QUEUE),
        ]
    return []


_RU_SHUFFLE_WORDS = ("перемешивание", "перемешку", "перемешка", "шафл", "шаффл")


def _close(word: str, target: str) -> bool:
    """Small Damerau-style edit budget for one long command noun."""
    if word == target:
        return True
    if abs(len(word) - len(target)) > 2 or len(word) < 6:
        return False
    import difflib
    return difflib.SequenceMatcher(None, word, target).ratio() >= 0.88


def _ru(value: str) -> str | None:
    tail = r"(?:\s+(?:на\s+(?:пк|компьютере|компе)|в\s+(?:яндекс\s+музыке|яндексе)|на\s+ютубе|в\s+ютубе|в\s+спотифа[йе]|в\s+дискорде))?"
    words = value.split()
    m = re.fullmatch(r"(включи|активируй|запусти|выключи|отключи|убери|сними)\s+(?:режим\s+)?(\S+)(" + tail + r")", value)
    if m and any(_close(m.group(2), target) for target in _RU_SHUFFLE_WORDS):
        on = m.group(1) in {"включи", "активируй", "запусти"}
        return (SHUFFLE_ON if on else SHUFFLE_OFF) + _ru_suffix(m.group(3))
    m = re.fullmatch(r"(?:перемешай|перемешать|перетасуй)\s+(?:мой\s+|мою\s+|этот\s+|эту\s+)?(?:плейлист\w*|очеред\w*|музык\w*|трек\w*|песн\w*)(" + tail + r")", value)
    if m:
        return SHUFFLE_ON + _ru_suffix(m.group(1))
    m = re.fullmatch(r"(?:(?:включи|поставь|сделай)\s+)?(?:повтор\w*|репит)\s+(плейлист\w*|очеред\w*|трек\w*|песн\w*)(" + tail + r")", value)
    if m:
        target = REPEAT_PLAYLIST if m.group(1).startswith(("плейлист", "очеред")) else REPEAT_TRACK
        return target + _ru_suffix(m.group(2))
    m = re.fullmatch(r"(?:поставь\s+)?(плейлист\w*|трек\w*|песн\w*)\s+на\s+повтор(" + tail + r")", value)
    if m:
        target = REPEAT_PLAYLIST if m.group(1).startswith("плейлист") else REPEAT_TRACK
        return target + _ru_suffix(m.group(2))
    del words
    return None


def _ru_suffix(tail: str) -> str:
    tail = str(tail or "").strip()
    if "яндекс" in tail:
        return _SUFFIX["yandex"]
    if "спотиф" in tail:
        return _SUFFIX["spotify"]
    if "дискорд" in tail:
        return _SUFFIX["discord"]
    return ""


@lru_cache(maxsize=8)
def _compiled(code: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    lead = _LEAD.get(code, "")
    polite = _POLITE.get(code, "")
    sources = "|".join(f"(?P<{name}>{pattern})" for name, pattern in _SOURCES.items())
    out = []
    for body, canonical in _rules(code):
        pattern = (rf"^{lead}(?:(?:{sources})\s+)?(?:{body})(?:\s+(?:{sources.replace('?P<', '?P<t_')}))?"
                   rf"(?:\s+{polite})*$")
        out.append((re.compile(pattern, re.I), canonical))
    return tuple(out)


def canonical_media_command(text: str, code: str) -> str | None:
    """Canonical internal music command for a complete native phrase, else None."""
    value = _norm(text)
    if not value or len(value) > 90 or any(ch in str(text or "") for ch in "\"«»“”"):
        return None
    code = str(code or "ru").casefold()
    if code == "ru":
        return _ru(value)
    if " " not in value:
        # Bare single words («next», «weiter», «indietro») keep their existing,
        # context-dependent routes (browser/back, dialogue answers).
        return None
    for pattern, canonical in _compiled(code):
        match = pattern.fullmatch(value)
        if not match:
            continue
        groups = {k for k, v in match.groupdict().items() if v}
        source = next((name for name in ("yandex", "spotify", "discord", "youtube", "pc")
                       if name in groups or ("t_" + name) in groups), "")
        return canonical + _SUFFIX.get(source, "")
    return None
