# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from jarvis.runtime.multilingual_commands import command_language_violation, normalize_multilingual_command


# HOTFIX118: this parser runs on the permanent wake stream, so every avoided
# regex compilation matters. Keep the exact legacy patterns/semantics while
# compiling static expressions once at import time.
_SPLIT_NEXT_RE = re.compile(r"\s*(?:daj\s+si[eę]|dai\s+sie|dajsie|deli[şsș]e|delishe|delsi)[\s.!?,;:-]*", re.I)
_SPLIT_BACK_RE = re.compile(r"\s*(?:на\s+зад|na\s+zad)[\s.!?,;:-]*", re.I)
_DISLIKE_SPLIT_RE = re.compile(r"\b(?:диз|дис|дез|дэз|дес)\s+лайк(?:ни)?\b", re.I)
_DISLIKE_EN_RE = re.compile(r"\bdis\s+like\b", re.I)
_THIS_LIKE_RE = re.compile(r"\s*this\s+(?:like|лайк)[\s.!?,;:-]*", re.I)
_NONWORD_RE = re.compile(r"[^a-zа-я0-9]+", re.I)
_WS_RE = re.compile(r"\s+")
_TRIM_RE = re.compile(r"^[\s.!?,;:\-]+|[\s.!?,;:\-]+$")
_RAW_FAST_RE = re.compile(r"[\s.!?,;:\-]+")
_SAFE_CROSSLINGUAL_RE = re.compile(
    r"(?:dalsze|dalshe|dalsz|dalsza|dalze|delsi|daj\s+si[eę]|dai\s+sie|dajsie|deli[şsș]e|delishe|دلشة|nazad|nazat|na\s+zad)[\s.!?,;:-]*",
    re.I,
)
_PAUSE_OBJECT_RE = re.compile(r"постав\w* (?:видео|видос|ролик|ютуб|ютьюб|youtube|музык\w*|трек\w*|песн\w*) на пауз\w*")
_PAUSE_SHORT_RE = re.compile(r"(?:видео|видос|ролик|ютуб|ютьюб|youtube|музык\w*|трек\w*|песн\w*) (?:пауза|стоп|стап)")


@dataclass(frozen=True)
class QuickMediaCommand:
    action: str  # pause/resume/next/previous/like/dislike/volume_up/volume_down/repeat_toggle/shuffle_toggle
    target: str  # auto/music/video/yandex_music/spotify/discord
    heard: str
    recognized: str = ""  # original accepted transcript; action still uses the canonical enum


# These are deliberately short hands-free commands.  False positives matter more
# than breadth because the parser runs on the permanent wake stream without an
# explicit wake word.  HOTFIX8 therefore keeps only two canonical NEXT intents -
# «дальше», «следующее» and «скип», with observed ASR spellings.
_FILLERS = {
    "ну", "давай", "пожалуйста", "a", "an", "эй", "плиз", "сейчас", "быстро", "прямо", "теперь",
    "еще", "ещё", "раз", "все", "всё", "ок", "okay", "ok",
    "джарвис", "jarvis", "jarwis", "джорвис", "дарвис", "ярвис",
    "нео", "нио", "нэо", "neo", "nio",
}

_VIDEO = {
    "видео", "видио", "видево", "видос", "ролик", "ролек", "ютуб", "ютюб", "ютюп", "ютьюб", "youtube", "ютубе", "ютьюбе",
    "video", "vidio", "yutub", "youtub",
}
_YANDEX = {
    "яндекс", "яндэкс", "яндикс", "яныдекс", "яндексс", "yandex", "yandeks", "yandexmusic",
    "яндексмузыка", "яндексмузика", "яндексмузыке", "яндексмузыку",
}
_SPOTIFY = {"spotify", "спотифай", "спотифи", "спотиф", "спотифае", "спотифае"}
_DISCORD = {
    "дискорд", "дискорт", "дискор", "дискордт", "дискорде", "дескорд", "дескорт", "детскорд", "детскорде",
    "discord", "discorde", "discort", "diskord", "diskort", "deskord", "descord",
    "дискордбот", "ботдискорд",
}
_MUSIC = {
    "музыка", "музыку", "музыки", "музык", "музика", "музику", "музон", "трек", "треки", "трэк", "трак", "трап", "песня", "песню", "песни", "песенка", "аудио",
    "music", "muzik", "muzika", "muzyka", "musika", "musyka", "muzyku", "muziku", "track", "trek", "song", "pesnya", "pesnia", "audio",
}

# Observed/likely deformations are grouped by semantic intent.  The total utterance
# is still constrained to a tiny vocabulary below, so a generous spelling set does
# not turn ordinary background conversation into a transport command.
_ACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dislike", re.compile(r"^(?:дизлайк(?:ни)?|дислайк(?:ни)?|дезлайк(?:ни)?|дэзлайк(?:ни)?|дизлаик(?:ни)?|дизлек|деслайк|дэзлек|dislike|dislaik|dislayk|dislay|dizlike|dezlike|thumbsdown)$", re.I)),
    ("like", re.compile(r"^(?:лайк(?:ни)?|лаик(?:ни)?|лайкк|like|alike|лайкэ)$", re.I)),
    ("pause", re.compile(
        r"^(?:стоп+|стап+|стоб|стопе|стопп|стопа|stop+|stap+|stopp?|stob|"
        r"пауза|паузу|пауз|пауса|паус|паузаа|pause|pauza|pausa|paws|poz|"
        r"приостанов\w*|останов\w*|замри)$", re.I)),
    ("resume", re.compile(
        r"^(?:продолж\w*|продалж\w*|прадолж\w*|продолжен\w*|продолжим|"
        r"возобнов\w*|возабнов\w*|вазобнов\w*|"
        r"prodolzh\w*|prodolj\w*|prodolji\w*|prodoljen\w*|vozobnov\w*|vozabnov\w*|"
        r"resume|resum|rezume|play|pley)$", re.I)),
    # Fast skip: exact next-track words remain safe because the whole utterance is constrained.
    ("next", re.compile(
        r"^(?:дальше|далше|дальш|далш|далъше|дальши|далши|дальжэ|далже|дальшеэ|дальшэ|дальшее|дальшеа|дальшо|"
        r"dalshe|dalsheh|dalsze|dalsz|dalze|dalsche|dalche|dalsch|dalsie|dalscha|dalsai|dalsay|dalsaj|dalszy|dalszi|dalsyi|dalsa|dalsza|dalsy|"
        r"следующее|следующий|следующая|следующую|следующии|следушее|следущий|следущая|следуший|"
        r"скип|скипни|скипнуть|скипа|скипп|скып|скыпни|скиб|скибни|скеп|"
        r"skip|skipp|skipit|skib|skyp|skep|skeep|forward|вперед|вперёд)$", re.I)),
    ("previous", re.compile(
        r"^(?:назад|назада|назат|назадд|назать|назадь|nazad|nazat|nazaad|back|bak|"
        r"предыдущ\w*|предущ\w*|предыд\w*|предид\w*|предуш\w*|предыдуще\w*|"
        r"predyd\w*|predid\w*|predush\w*|previous|previus|prev|prew|"
        r"прошл\w*|верни|вернут\w*)$", re.I)),
)


def _norm(text: str) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    # Exact field-observed renderings of Russian «дальше». Keep these as
    # whole short-command rewrites so ordinary Polish/Turkish conversation does
    # not become a no-wake transport command.
    value = value.replace("دلشة", " dalsze ")
    if _SPLIT_NEXT_RE.fullmatch(value):
        value = "dalsze"
    if _SPLIT_BACK_RE.fullmatch(value):
        value = "назад"
    # Whisper regularly splits Russian «дизлайк» into two words ("Дез лайк").
    # Collapse only the tiny observed command family before token validation so
    # Discord/Yandex auto-targeting remains identical to a clean «дизлайк».
    value = _DISLIKE_SPLIT_RE.sub(" дизлайк ", value)
    value = _DISLIKE_EN_RE.sub(" dislike ", value)
    # In the Russian profile Whisper has also returned the complete short command
    # as English "this like".  Accept it only when it is the whole utterance;
    # never rewrite those words inside normal conversation.
    if _THIS_LIKE_RE.fullmatch(value):
        value = "dislike"
    value = _NONWORD_RE.sub(" ", value)
    return _WS_RE.sub(" ", value).strip()


_LANGUAGE_FILLERS: dict[str, set[str]] = {
    "en": {"the", "a", "an", "please", "now"},
    "de": {"die", "der", "das", "den", "dem", "ein", "eine", "einen", "bitte", "jetzt"},
    "fr": {"le", "la", "les", "un", "une", "du", "de", "des", "s'il", "sil", "te", "plaît", "plait", "maintenant"},
    "es": {"el", "la", "los", "las", "un", "una", "por", "favor", "ahora"},
    "it": {"il", "lo", "la", "i", "gli", "le", "un", "una", "per", "favore", "ora"},
    "pt": {"o", "a", "os", "as", "um", "uma", "por", "favor", "agora"},
    "ru": set(),
}
_FILLERS_BY_LANGUAGE: dict[str, frozenset[str]] = {
    code: frozenset(_FILLERS | values) for code, values in _LANGUAGE_FILLERS.items()
}
_DEFAULT_FILLERS = frozenset(_FILLERS)

def _compact_tokens(text: str, language: str = "ru") -> list[str]:
    code = str(language or "ru").casefold()
    fillers = _FILLERS_BY_LANGUAGE.get(code, _DEFAULT_FILLERS)
    return [token for token in _norm(text).split() if token and token not in fillers]


def _safe_crosslingual_transport_alias(text: str, language: str = "ru") -> bool:
    """RU-only ASR repair for observed renderings of Russian «дальше/назад».

    These spellings are acoustic transcription artifacts, not an English/foreign
    command fallback. Other selected languages must use their own command atlas.
    """
    if str(language or "ru").casefold() != "ru":
        return False
    value = str(text or "").casefold().strip()
    return _SAFE_CROSSLINGUAL_RE.fullmatch(value) is not None


# HOTFIX105: O(1) locale-local exact transport layer. These are the most common
# no-wake commands and are checked before alias/fuzzy normalization. No foreign
# locale dictionary is consulted, and proper names are not involved here.
_LOCALE_FAST_EXACT: dict[str, dict[str, str]] = {
    "ru": {"стоп":"pause","пауза":"pause","продолжи":"resume","продолжить":"resume","возобнови":"resume","дальше":"next","вперед":"next","вперёд":"next","следующий":"next","следующее":"next","скип":"next","назад":"previous","предыдущий":"previous","предыдущее":"previous","лайк":"like","дизлайк":"dislike","громче":"volume_up","погромче":"volume_up","тише":"volume_down","потише":"volume_down","повтор":"repeat_toggle","повтори":"repeat_toggle","репит":"repeat_toggle","перемешай":"shuffle_toggle","перемешивание":"shuffle_toggle","шаффл":"shuffle_toggle"},
    "en": {"stop":"pause","pause":"pause","play":"resume","resume":"resume","next":"next","skip":"next","back":"previous","previous":"previous","like":"like","dislike":"dislike"},
    "de": {"stopp":"pause","stop":"pause","pause":"pause","pausiere":"pause","fortsetzen":"resume","fortsetzen bitte":"resume","nächster":"next","nächstes":"next","weiterer titel":"next","zurück":"previous","vorheriger":"previous","vorheriges":"previous","gefällt mir":"like","gefällt mir nicht":"dislike"},
    "fr": {"stop":"pause","pause":"pause","mets en pause":"pause","reprends":"resume","continue":"resume","suivant":"next","suivante":"next","prochain":"next","prochaine":"next","précédent":"previous","précédente":"previous","retour":"previous","j'aime":"like","j’aime":"like","je n'aime pas":"dislike","je n’aime pas":"dislike"},
    "es": {"stop":"pause","pausa":"pause","detén":"pause","deten":"pause","reanuda":"resume","continúa":"resume","continua":"resume","siguiente":"next","próximo":"next","proximo":"next","anterior":"previous","atrás":"previous","atras":"previous","me gusta":"like","no me gusta":"dislike"},
    "it": {"stop":"pause","pausa":"pause","ferma":"pause","riprendi":"resume","continua":"resume","successivo":"next","prossimo":"next","precedente":"previous","indietro":"previous","mi piace":"like","non mi piace":"dislike"},
    "pt": {"stop":"pause","pausa":"pause","pare":"pause","retome":"resume","continue":"resume","próxima":"next","proxima":"next","próximo":"next","proximo":"next","anterior":"previous","voltar":"previous","curtir":"like","gostei":"like","não curtir":"dislike","nao curtir":"dislike","não gostei":"dislike","nao gostei":"dislike"},
}

def _fast_locale_action(text: str, code: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
    value = _TRIM_RE.sub("", value)
    value = _WS_RE.sub(" ", value)
    action = _LOCALE_FAST_EXACT.get(code, {}).get(value, "")
    if action:
        return action
    if code == 'ru' and len(value) <= 28:
        repaired = _norm(value)
        for name, pattern in _ACTION_PATTERNS:
            if name in {'next', 'previous'} and pattern.fullmatch(repaired):
                return name
    return ""


def parse_quick_media_command(text: str, language: str = "ru") -> QuickMediaCommand | None:
    """Parse a short no-wake transport command with conservative semantics."""

    heard = str(text or "").strip()
    code = str(language or "ru").casefold()
    if code not in {"ru", "en", "de", "fr", "es", "it", "pt"}:
        code = "ru"
    fast_action = _fast_locale_action(heard, code)
    if fast_action:
        return QuickMediaCommand(action=fast_action, target="auto", heard=heard)
    if code == "ru":
        exact = _norm(heard)
        explicit = {
            "сделай громче": "volume_up", "музыка громче": "volume_up",
            "сделай тише": "volume_down", "музыка тише": "volume_down",
            "включи повтор": "repeat_toggle", "выключи повтор": "repeat_toggle",
            "включи перемешивание": "shuffle_toggle", "выключи перемешивание": "shuffle_toggle",
        }.get(exact)
        if explicit:
            return QuickMediaCommand(action=explicit, target="music", heard=heard)
    # HOTFIX119: transport words are intentionally universal control vocabulary.
    # They do not participate in Phrase-Atlas ownership and are safe only because
    # the whole utterance must be exactly one short media-control token. This
    # restores "next / stop / back" while a RU UI is selected without reopening
    # cross-language routing for ordinary commands.
    raw_fast = _RAW_FAST_RE.sub("", heard.casefold())
    raw_fast_actions = {
        "stop": "pause",
        "pause": "pause",
        "play": "resume",
        "resume": "resume",
        "continue": "resume",
        "next": "next",
        "forward": "next",
        "skip": "next",
        "back": "previous",
        "previous": "previous",
        "prev": "previous",
        "like": "like",
        "dislike": "dislike",
        "louder": "volume_up",
        "quieter": "volume_down",
        "shuffle": "shuffle_toggle",
        "repeat": "repeat_toggle",
    }
    if raw_fast in raw_fast_actions:
        return QuickMediaCommand(action=raw_fast_actions[raw_fast], target="auto", heard=heard)

    violation, _detected = command_language_violation(heard, code)
    if violation and not _safe_crosslingual_transport_alias(heard, code):
        return None

    normalized = normalize_multilingual_command(heard, code)
    tokens = _compact_tokens(normalized, code)
    if not tokens or len(tokens) > 6:
        return None

    target = "auto"
    token_set = set(tokens)
    if token_set & _VIDEO:
        target = "video"
    if token_set & _YANDEX:
        if target == "video": return None
        target = "yandex_music"
    if token_set & _SPOTIFY:
        if target not in {"auto", "music"}: return None
        target = "spotify"
    if token_set & _DISCORD:
        if target in {"video", "yandex_music", "spotify"}:
            return None
        target = "discord"
    if target == "auto" and token_set & _MUSIC:
        target = "music"

    allowed = _VIDEO | _YANDEX | _SPOTIFY | _DISCORD | _MUSIC | {
        "на", "поставь", "поставить", "текущий", "текущую", "текущее", "один",
        "вперед", "вперёд", "пожалуйста", "плиз", "воспроизведение", "воспроизведени", "playback",
    }
    action = ""
    action_index = -1
    priority = {"dislike": 50, "like": 45, "resume": 42, "pause": 40, "next": 35, "previous": 35}
    best_score = -1
    for index, token in enumerate(tokens):
        for candidate, pattern in _ACTION_PATTERNS:
            if pattern.fullmatch(token):
                score = priority[candidate]
                if score > best_score:
                    action, action_index, best_score = candidate, index, score

    # Natural phrase «поставь ... на паузу» remains safe because the media object
    # is explicit and the actual action is pause.
    if not action:
        joined = " ".join(tokens)
        if _PAUSE_OBJECT_RE.fullmatch(joined):
            action = "pause"
        elif _PAUSE_SHORT_RE.fullmatch(joined):
            action = "pause"
        else:
            return None

    for index, token in enumerate(tokens):
        if index == action_index or token in allowed:
            continue
        if any(pattern.fullmatch(token) for _, pattern in _ACTION_PATTERNS):
            continue
        return None

    # HOTFIX105: no cross-language transport fallback. The selected locale owns
    # its no-wake vocabulary; English transport words are valid only in EN mode.

    if tokens == ["верни"]:
        return None
    return QuickMediaCommand(action=action, target=target, heard=heard)


QUICK_MEDIA_HELP = (
    "Без wake word используются транспортные фразы выбранного языка: пауза / продолжить / дальше / назад / лайк / дизлайк / громче / тише / повтор / перемешай. "
    "Можно уточнить источник: стоп музыка, пауза Discord, продолжи Яндекс, пауза Spotify, стоп видео. "
    "Короткие транспортные команды исполняются напрямую и не требуют wake word."
)
