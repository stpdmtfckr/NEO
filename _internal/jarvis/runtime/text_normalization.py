# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import difflib
import re
import unicodedata
from functools import lru_cache

# Visual Latin/Cyrillic confusables that commonly appear as a *single* stray
# character in Whisper output.  We only repair a token when one script clearly
# dominates the token; complete English/Russian words are left intact.
_LAT_TO_CYR = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у", "k": "к", "m": "м", "t": "т", "b": "в", "h": "н",
}
_CYR_TO_LAT = {value: key for key, value in _LAT_TO_CYR.items()}

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+", re.UNICODE)
_SPACE_RE = re.compile(r"[ \t]+")

_COMMAND_LEXICON = {
    "открой", "запусти", "покажи", "выведи", "закрой", "перейди", "переключи",
    "включи", "выключи", "продолжи", "возобнови", "воспроизведение", "поставь",
    "пауза", "паузу", "видео", "ролик", "браузер", "вкладка", "вкладку", "окно",
    "перенеси", "перемести", "монитор", "экран", "приложение", "программу", "найди",
    "поищи", "информация", "информацию", "яндекс", "музыка", "музыку", "песня",
    "песню", "трек", "дискорд", "стим", "сервер", "канал", "музыкальный", "бот",
}

_COMMAND_LEXICON_FOLDED = tuple(x.casefold().replace("ё", "е") for x in _COMMAND_LEXICON)

@lru_cache(maxsize=4096)
def _lexical_repair(token: str) -> str:
    latin, cyr = _script_counts(token)
    if not (latin and cyr):
        return token
    folded = token.casefold().replace("ё", "е")
    best = ""
    best_score = 0.0
    for ref in _COMMAND_LEXICON_FOLDED:
        score = difflib.SequenceMatcher(None, folded, ref).ratio()
        if score > best_score:
            best, best_score = ref, score
    if best and best_score >= 0.78 and abs(len(best) - len(folded)) <= 1:
        if token[:1].isupper():
            return best[:1].upper() + best[1:]
        return best
    return token


@lru_cache(maxsize=8192)
def _script_counts(token: str) -> tuple[int, int]:
    latin = 0
    cyr = 0
    for ch in token:
        folded = ch.casefold()
        if "a" <= folded <= "z":
            latin += 1
        elif ("а" <= folded <= "я") or folded == "ё":
            cyr += 1
    return latin, cyr


@lru_cache(maxsize=8192)
def repair_mixed_script_token(token: str) -> str:
    """Repair a single stray confusable character inside a dominant-script word.

    Examples: ``паyза`` -> ``пауза`` and ``gоg`` -> ``gog``.  A real English
    word next to Russian text, e.g. ``открой GOG Galaxy``, is untouched because
    each token itself is single-script.
    """
    if not token:
        return token
    latin, cyr = _script_counts(token)
    if cyr >= 2 and latin == 1:
        chars = []
        changed = False
        for ch in token:
            mapped = _LAT_TO_CYR.get(ch.casefold())
            if mapped and "a" <= ch.casefold() <= "z":
                chars.append(mapped.upper() if ch.isupper() else mapped)
                changed = True
            else:
                chars.append(ch)
        if changed:
            return "".join(chars)
    if latin >= 2 and cyr == 1:
        chars = []
        changed = False
        for ch in token:
            mapped = _CYR_TO_LAT.get(ch.casefold())
            if mapped and (("а" <= ch.casefold() <= "я") or ch.casefold() == "ё"):
                chars.append(mapped.upper() if ch.isupper() else mapped)
                changed = True
            else:
                chars.append(ch)
        if changed:
            return "".join(chars)
    return _lexical_repair(token)


def repair_mixed_script_text(text: str) -> str:
    """NFC-normalize text and repair only high-confidence one-character script slips."""
    value = unicodedata.normalize("NFC", str(text or ""))
    return _TOKEN_RE.sub(lambda m: repair_mixed_script_token(m.group(0)), value)


def normalize_spoken_command(text: str) -> str:
    """Conservative normalization used for intent parsing.

    We deliberately do *not* transliterate whole English words.  This keeps app,
    game, song and channel names such as GOG Galaxy, Discord and RedCheck intact.
    """
    value = repair_mixed_script_text(text)
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    return _SPACE_RE.sub(" ", value).strip()
